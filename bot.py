import os
import re
import uuid
import json
import zipfile
import time
import html as html_lib
import asyncio
import urllib.parse
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup, NavigableString, Comment, Doctype, Declaration, ProcessingInstruction
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

IDS_LIBERADOS = {
    8672397104,
    1130170420,
}

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

MARCA_IMAGEM = BASE_DIR / "alma_scriptum.png"

usuarios = {}
cancelamentos = set()

MERGE_LENGTH = 3800  # aumentado para dar mais contexto ao Google sem pesar demais
# O plugin Ebook Translator usa separador de parágrafo por duas quebras de linha.
# Mantemos isso para ficar mais parecido com o Calibre.
CALIBRE_SEPARATOR = "\n\n"
REQUEST_ATTEMPTS = 1
REQUEST_TIMEOUT = 15
REQUEST_INTERVAL = 0.005

CONFIGS = {
    "google_new": {"nome": "🌐 Google Free New", "workers": 10},
    "google_html": {"nome": "📄 Google Free HTML", "workers": 6},
    "google_old": {"nome": "🕰️ Google Free Old", "workers": 4},
}

SEP_TEMPLATE = "{{{{id_{}}}}}"


def usuario_liberado(user_id):
    return user_id in IDS_LIBERADOS


def limpar_nome(nome):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", nome)


def criar_nome_final(nome_original):
    nome = Path(nome_original).stem

    nome = nome.replace("_", " ")
    nome = nome.replace("-", " ")
    nome = re.sub(r"\s*\([^)]*\)", " ", nome)

    sujeiras = [
        r"z[\s\-_]*library[\s\._\-]*sk",
        r"z[\s\-_]*lib[\s\._\-]*sk",
        r"z[\s\-_]*lib",
        r"1lib[\s\._\-]*sk",
        r"1lib",
        r"sk",
        r"oceanofpdf[\s\._\-]*com",
        r"oceanofpdf",
        r"ocean pdf",
        r"pt[\s\-_]*br",
        r"ptbr",
        r"br[\s\-_]*pt[\s\-_]*br",
        r"\[pt-br\]",
        r"alma scriptum translate",
        r"alma scriptum",
        r"translate",
        r"traduzido",
        r"revisado",
    ]

    for item in sujeiras:
        nome = re.sub(item, " ", nome, flags=re.IGNORECASE)

    nome = re.sub(r"[,;:]+", " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip()

    if not nome:
        nome = "Livro"

    return f"{nome} - [PT-BR] - Alma Scriptum.epub"


def nome_mecanismo(mecanismo):
    return CONFIGS.get(mecanismo, CONFIGS["google_new"])["nome"]


def teclado_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Google New", callback_data="set_google_new")],
        [
            InlineKeyboardButton("📄 Google HTML", callback_data="set_google_html"),
            InlineKeyboardButton("🕰️ Google Old", callback_data="set_google_old"),
        ],
        [
            InlineKeyboardButton("🖼️ Marca", callback_data="marca"),
            InlineKeyboardButton("🛑 Cancelar", callback_data="cancelar"),
        ],
    ])


def barra_progresso(porcentagem):
    cheios = porcentagem // 10
    vazios = 10 - cheios
    return "🟩" * cheios + "⬜" * vazios


def texto_curto(texto, limite=420):
    texto = re.sub(r"\s+", " ", str(texto)).strip()
    if len(texto) > limite:
        return texto[:limite].strip() + "..."
    return texto


def substituir_sites_por_marca(texto):
    if not texto:
        return texto

    texto = str(texto)

    padroes = [
        r"Ocean\s*of\s*PDF\.?\s*com",
        r"OceanofPDF\.?\s*com",
        r"OceanPDF\.?\s*com",
        r"oceanofpdf\.com",
        r"oceanofpdf",
        r"OceanofPDF",
        r"Ocean\s*Of\s*PDF",
        r"Ocean\s*PDF",
        r"z[\s\-_]*library(?:\.[a-z]{2,})?",
        r"z[\s\-_]*lib(?:\.[a-z]{2,})?",
        r"zlib",
        r"1lib(?:\.[a-z]{2,})?",
        r"libgen(?:\.[a-z]{2,})?",
        r"anna['’]?s[\s\-_]*archive",
        r"https?://(?:www\.)?(?:oceanofpdf|z-library|z-lib|1lib|libgen|wattpad|img\.wattpad)[^\s<>'\"]*",
        r"www\.(?:oceanofpdf|z-library|z-lib|1lib|libgen|wattpad)[^\s<>'\"]*",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.IGNORECASE)

    texto = re.sub(r"\b[A-Za-z0-9]{60,}\b", "", texto)
    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    return texto.strip()

def revisar_texto_final(texto):
    """
    Pós-processamento mínimo, estilo Calibre.
    Aqui NÃO fazemos correções agressivas de tradução, porque isso pode piorar
    o sentido. Apenas limpamos entidades, sites e espaçamento quebrado.
    """
    if not texto:
        return texto

    texto = html_lib.unescape(str(texto))
    texto = substituir_sites_por_marca(texto)

    texto = texto.replace("&quot;", '"')
    texto = texto.replace("&#39;", "'")
    texto = texto.replace("&amp;", "&")

    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"([,.!?;:])([A-Za-zÀ-ÿ])", r"\1 \2", texto)
    texto = re.sub(r"([“\"])([A-Za-zÀ-ÿ])", r"\1 \2", texto)
    texto = re.sub(r"([A-Za-zÀ-ÿ])([”\"])", r"\1\2", texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()

def detectar_contexto_original(texto):
    """Detecta contexto no texto original para corrigir ambiguidades."""
    t = str(texto or "").lower()

    armas = [
        "gun", "guns", "shot", "shots", "shoot", "shooting", "shooter",
        "fired", "firearm", "rifle", "pistol", "revolver", "bullet",
        "bullets", "trigger", "barrel", "ammo", "weapon", "weapons",
        "aim", "aimed", "target", "missed", "hit", "muzzle", "holster"
    ]

    futebol = [
        "soccer", "football", "goal", "ball", "kick", "kicked",
        "field", "goalkeeper", "match", "team", "penalty"
    ]

    contexto = set()

    if sum(1 for p in armas if re.search(rf"\b{re.escape(p)}\b", t)) >= 1:
        contexto.add("armas")

    if sum(1 for p in futebol if re.search(rf"\b{re.escape(p)}\b", t)) >= 2:
        contexto.add("futebol")

    return contexto


def corrigir_coerencia_contextual(original, traduzido):
    """Correção leve pós-tradução baseada no original."""
    if not traduzido:
        return traduzido

    texto = str(traduzido)
    original_l = str(original or "").lower()
    contexto = detectar_contexto_original(original)

    if "armas" in contexto and "futebol" not in contexto:
        if re.search(r"\bshot(s)?\b", original_l) or re.search(r"\bfired\b", original_l):
            texto = re.sub(r"\bchute\b", "tiro", texto, flags=re.I)
            texto = re.sub(r"\bchutes\b", "tiros", texto, flags=re.I)
            texto = re.sub(r"\bpontapé\b", "tiro", texto, flags=re.I)
            texto = re.sub(r"\bpontapés\b", "tiros", texto, flags=re.I)

        if re.search(r"\bmissed\b", original_l):
            texto = re.sub(r"\bsaiu para fora\b", "errou o alvo", texto, flags=re.I)
            texto = re.sub(r"\berrou\b(?! o alvo)", "errou o alvo", texto, flags=re.I)

    correcoes = {
        "deTODOS": "de TODOS",
        "TODOS.Cada": "TODOS. Cada",
        "processo.A": "processo. A",
        "trama.Bruxas": "trama. Bruxas",
        "completaPara": "completa. Para",
        "bemEspero": "bem? Espero",
        "físicaSem": "física. Sem",
        "fisicaSem": "física. Sem",
        "semviolência": "sem violência",
        "semviolencia": "sem violência",
        "lágri mas": "lágrimas",
        "lá gri mas": "lágrimas",
        "gr ito": "grito",
        "TO grito": "O grito",
        "TO gr ito": "O grito",
        "T O grito": "O grito",
        "memó ria": "memória",
        "fí sica": "física",
        "rá pido": "rápido",
        "cére bro": "cérebro",
        "conse guir": "conseguir",
        "sozin has": "sozinhas",
        "emesse": "em esse",
        "nessequarto": "nesse quarto",
    }

    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)

    texto = re.sub(r"([a-záàâãéêíóôõúç])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})", r"\1 \2", texto)
    texto = re.sub(r"([.!?;:])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇA-Za-zÀ-ÿ])", r"\1 \2", texto)
    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto)

    return texto.strip()



# ==================================================
# MEMÓRIA GLOBAL + REVISOR SEGURO DE GÊNERO — VERSÃO 4
# ==================================================
# NÃO usa IA externa.
# Foi feito para romance, fantasia, harém reverso e dual/multi POV.
# Ideia principal:
# - aprende personagens automaticamente durante o EPUB;
# - guarda memória temporária do livro inteiro;
# - detecta POV por capítulo;
# - identifica narrador externo/autora;
# - corrige com força apenas primeira pessoa;
# - corrige apelidos por relação/proximidade;
# - usa sistema de confiança para não destruir cenas confusas.

MODO_FANTASIA_SEGURO = True
CONFIANCA_CORRIGIR_NORMAL = 7
CONFIANCA_CORRIGIR_FANTASIA = 10

PARES_GENERO_ADJETIVOS = [
    ("irritado", "irritada"), ("bravo", "brava"), ("zangado", "zangada"),
    ("cansado", "cansada"), ("exausto", "exausta"), ("pronto", "pronta"),
    ("sozinho", "sozinha"), ("preocupado", "preocupada"), ("nervoso", "nervosa"),
    ("animado", "animada"), ("confuso", "confusa"), ("culpado", "culpada"),
    ("ferrado", "ferrada"), ("apaixonado", "apaixonada"), ("ocupado", "ocupada"),
    ("assustado", "assustada"), ("atrasado", "atrasada"), ("chateado", "chateada"),
    ("decepcionado", "decepcionada"), ("envergonhado", "envergonhada"),
    ("orgulhoso", "orgulhosa"), ("obrigado", "obrigada"), ("bonito", "bonita"),
    ("lindo", "linda"), ("doido", "doida"), ("louco", "louca"),
    ("machucado", "machucada"), ("ferido", "ferida"), ("surpreso", "surpresa"),
    ("tenso", "tensa"), ("perdido", "perdida"), ("fodido", "fodida"),
]

FEM_DIALOGO_FORTE = [
    "princesa", "filha", "menina", "garota", "pequena", "querida", "mocinha",
    "minha garota", "minha menina", "minha filha", "jovem senhora", "bebê menina",
    "garotinha", "princesinha", "senhorita",
]

MASC_DIALOGO_FORTE = [
    "príncipe", "filho", "menino", "garoto", "pequeno", "querido", "mocinho",
    "meu garoto", "meu menino", "meu filho", "jovem senhor", "garotinho", "rapazinho",
]

MASC_BLOQUEIO_REAL = [
    "meu pênis", "meu penis", "meu pau", "minha barba", "sou homem", "sou um homem",
    "sou cara", "sou um cara", "o maior cara", "pai solteiro", "marido", "namorado",
    "tio", "rapaz adulto", "meu peito masculino", "cueca boxer",
]

FEM_BLOQUEIO_REAL = [
    "meu sutiã", "meu sutia", "minha calcinha", "estou grávida", "estou gravida",
    "sou mulher", "sou uma mulher", "sou garota", "sou uma garota", "mãe solteira",
    "mae solteira", "esposa", "namorada", "tia", "menstruação", "menstruacao",
]

PALAVRAS_FANTASIA_CONFUSA = [
    "magia", "mágico", "magica", "feiticeiro", "feiticeira", "bruxa", "bruxo", "fada",
    "fae", "vampiro", "vampira", "lobo", "lobisomem", "dragão", "dragao", "demônio",
    "demonio", "deusa", "deus", "alma", "corpo", "troca de corpo", "possessão", "possessao",
    "metamorfo", "shifter", "mate", "vínculo", "vinculo", "harém", "harem", "reverso",
]

RELACOES_FEM_ORIGINAL = [
    r"\bprincess\b", r"\bdaughter\b", r"\bgirl\b", r"\blittle girl\b", r"\bsweet girl\b",
    r"\bmy girl\b", r"\bbaby girl\b", r"\bmommy\b", r"\bmother\b", r"\bwife\b",
    r"\bgirlfriend\b", r"\bsister\b", r"\baunt\b",
]

RELACOES_MASC_ORIGINAL = [
    r"\bprince\b", r"\bson\b", r"\bboy\b", r"\blittle boy\b", r"\bmy boy\b",
    r"\bbaby boy\b", r"\bdaddy\b", r"\bfather\b", r"\bhusband\b", r"\bboyfriend\b",
    r"\bbrother\b", r"\buncle\b",
]

APELIDOS_NEUTROS_ORIGINAL = r"\b(kid|squirt|honey|sweetheart|little one|baby|darling|dear|love)\b"


def criar_memoria_livro():
    return {
        "personagens": {},       # nome -> {M:int, F:int}
        "relacoes": {},          # nome -> lista de relações detectadas
        "historico_livro": "",
        "pov_capitulos": {},
        "ultimo_pov": None,
        "fantasia": False,
    }


def _contar_padroes(texto, padroes):
    total = 0
    texto = str(texto or "")
    for p in padroes:
        if re.search(p, texto, flags=re.I):
            total += 1
    return total


def _match_case(novo, antigo):
    if antigo.isupper():
        return novo.upper()
    if antigo[:1].isupper():
        return novo[:1].upper() + novo[1:]
    return novo


def _trocar_palavra(texto, de, para):
    def repl(m):
        return _match_case(para, m.group(0))
    return re.sub(rf"\b{re.escape(de)}\b", repl, texto, flags=re.I)


def _texto_indica_feminino(texto):
    t = str(texto or "").lower()
    return any(p in t for p in FEM_DIALOGO_FORTE)


def _texto_indica_masculino(texto):
    t = str(texto or "").lower()
    return any(p in t for p in MASC_DIALOGO_FORTE)


def _tem_bloqueio_masculino_real(texto):
    t = str(texto or "").lower()
    return any(p in t for p in MASC_BLOQUEIO_REAL)


def _tem_bloqueio_feminino_real(texto):
    t = str(texto or "").lower()
    return any(p in t for p in FEM_BLOQUEIO_REAL)


def detectar_cena_fantasia_ou_confusa(original_all, traduzido_all):
    t = (str(original_all or "") + " " + str(traduzido_all or "")).lower()
    return any(p in t for p in PALAVRAS_FANTASIA_CONFUSA)


def extrair_nomes_provaveis(texto):
    """Extrai nomes próprios prováveis, evitando palavras comuns de início de frase."""
    texto = str(texto or "")
    comuns = {
        "Chapter", "Prologue", "Epilogue", "The", "And", "But", "For", "This", "That", "There",
        "Then", "When", "Where", "What", "Why", "How", "He", "She", "They", "We", "You", "I",
        "His", "Her", "My", "Your", "A", "An", "In", "On", "At", "To", "From", "With",
    }
    nomes = []
    for m in re.finditer(r"\b[A-Z][a-zA-Z'’\-]{2,}\b", texto):
        nome = m.group(0)
        if nome in comuns:
            continue
        if nome.lower() in ["google", "alma", "scriptum"]:
            continue
        nomes.append(nome)
    return nomes[:20]


def reforcar_personagem(memoria_livro, nome, genero, pontos=2, relacao=None):
    if not memoria_livro or not nome or genero not in ["M", "F"]:
        return
    nome = str(nome).strip()
    if len(nome) < 2:
        return
    personagens = memoria_livro.setdefault("personagens", {})
    dados = personagens.setdefault(nome, {"M": 0, "F": 0})
    dados[genero] = min(80, dados.get(genero, 0) + pontos)
    if relacao:
        memoria_livro.setdefault("relacoes", {}).setdefault(nome, [])
        if relacao not in memoria_livro["relacoes"][nome]:
            memoria_livro["relacoes"][nome].append(relacao)


def genero_personagem(memoria_livro, nome):
    if not memoria_livro or not nome:
        return None, 0
    dados = memoria_livro.get("personagens", {}).get(nome)
    if not dados:
        return None, 0
    m = dados.get("M", 0)
    f = dados.get("F", 0)
    if f >= m + 3 and f >= 4:
        return "F", f
    if m >= f + 3 and m >= 4:
        return "M", m
    return None, max(m, f)


def aprender_personagens_do_trecho(original, traduzido, memoria_livro):
    if not memoria_livro:
        return
    original = str(original or "")
    traduzido = str(traduzido or "")
    junto_l = (original + " " + traduzido).lower()
    nomes = extrair_nomes_provaveis(original)

    # Se uma frase tem nome + she/her/princess/daughter/girl, reforça feminino.
    # Se tem nome + he/him/prince/son/boy, reforça masculino.
    for nome in nomes:
        pad_nome = re.escape(nome)
        janela_nome = re.search(rf"\b{pad_nome}\b(.{{0,120}})", original, flags=re.I | re.S)
        janela = janela_nome.group(0).lower() if janela_nome else original.lower()

        if re.search(r"\b(she|her|hers|princess|daughter|girl|mommy|mother|wife|girlfriend|sister)\b", janela):
            reforcar_personagem(memoria_livro, nome, "F", 3, "pista feminina")
        if re.search(r"\b(he|him|his|prince|son|boy|daddy|father|husband|boyfriend|brother)\b", janela):
            reforcar_personagem(memoria_livro, nome, "M", 3, "pista masculina")

    # Reforça com relações sem nome específico no histórico do livro.
    if any(re.search(p, original, flags=re.I) for p in RELACOES_FEM_ORIGINAL) or _texto_indica_feminino(traduzido):
        memoria_livro["historico_livro"] = (memoria_livro.get("historico_livro", "") + " FEM_REL " + traduzido)[-50000:]
    if any(re.search(p, original, flags=re.I) for p in RELACOES_MASC_ORIGINAL) or _texto_indica_masculino(traduzido):
        memoria_livro["historico_livro"] = (memoria_livro.get("historico_livro", "") + " MASC_REL " + traduzido)[-50000:]

    if detectar_cena_fantasia_ou_confusa(original, traduzido):
        memoria_livro["fantasia"] = True


def _pontuar_genero_pov(original, traduzido):
    original_l = str(original or "").lower()
    traduzido_l = str(traduzido or "").lower()

    score_m = 0
    score_f = 0

    pistas_m_original = [
        r"\bmy\s+(?:dick|cock|penis)\b", r"\bboxer\s+briefs?\b",
        r"\bi'?m\s+(?:a\s+)?(?:man|guy|boy|father|dad|husband|boyfriend|uncle)\b",
        r"\bsingle\s+dad\b", r"\bmy\s+best\s+friend\b.{0,120}\b(?:he|him|his)\b",
    ]
    pistas_f_original = [
        r"\bmy\s+(?:bra|pussy|period)\b", r"\bi'?m\s+(?:a\s+)?(?:woman|girl|mother|mom|wife|girlfriend|aunt)\b",
        r"\bpregnan(?:t|cy)\b", r"\bsingle\s+mom\b", r"\bmy\s+best\s+friend\b.{0,120}\b(?:she|her|hers)\b",
    ]
    pistas_m_pt = [
        r"\bmeu\s+p[eê]nis\b", r"\bmeu\s+pau\b", r"\bcueca\s+boxer\b",
        r"\bsou\s+(?:um\s+)?(?:homem|cara|pai|marido|namorado|tio)\b", r"\bo\s+maior\s+cara\b",
        r"\bpai\s+solteiro\b", r"\bmeu\s+melhor\s+amigo\b",
    ]
    pistas_f_pt = [
        r"\bmeu\s+suti[ãa]\b", r"\bminha\s+calcinha\b", r"\bestou\s+gr[aá]vida\b",
        r"\bsou\s+(?:uma\s+)?(?:mulher|garota|mãe|mae|esposa|namorada|tia)\b",
        r"\bmãe\s+solteira\b", r"\bminha\s+melhor\s+amiga\b",
    ]

    score_m += 6 * _contar_padroes(original_l, pistas_m_original)
    score_f += 6 * _contar_padroes(original_l, pistas_f_original)
    score_m += 6 * _contar_padroes(traduzido_l, pistas_m_pt)
    score_f += 6 * _contar_padroes(traduzido_l, pistas_f_pt)

    # Pistas de diálogo contam pouco porque podem ser outro personagem.
    score_f += sum(1 for p in FEM_DIALOGO_FORTE if p in traduzido_l)
    score_m += sum(1 for p in MASC_DIALOGO_FORTE if p in traduzido_l)

    return score_m, score_f


def detectar_narrador_externo(originais, traduzidas):
    """Detecta terceira pessoa/narrador externo para evitar forçar gênero do 'eu'."""
    texto = (" ".join(str(x or "") for x in originais) + " " + " ".join(str(x or "") for x in traduzidas)).lower()
    primeira = len(re.findall(r"\b(i|i'm|i’ve|i'll|my|me|eu|meu|minha|estou|sou|fiquei|vou)\b", texto, flags=re.I))
    terceira = len(re.findall(r"\b(she|her|he|him|his|ela|ele|dela|dele|sua|seu)\b", texto, flags=re.I))
    dialogos = texto.count('"') + texto.count('“') + texto.count('”')
    if terceira >= primeira * 3 + 8 and primeira <= 8:
        return True
    if terceira >= 25 and primeira <= 4 and dialogos < 12:
        return True
    return False


def detectar_pov_global_capitulo(originais, traduzidas, memoria_livro=None, capitulo=""):
    original_all = " ".join(str(x or "") for x in originais).lower()
    traducao_all = " ".join(str(x or "") for x in traduzidas).lower()

    if detectar_narrador_externo(originais, traduzidas):
        return "EXTERNO", 0

    score_m, score_f = _pontuar_genero_pov(original_all, traducao_all)

    # Nomes de capítulo às vezes são o nome do POV. Se já aprendemos o personagem, usa como pista.
    if memoria_livro and capitulo:
        for nome in extrair_nomes_provaveis(capitulo):
            g, conf = genero_personagem(memoria_livro, nome)
            if g == "M":
                score_m += min(10, conf)
            elif g == "F":
                score_f += min(10, conf)

    # Detecta capítulos por título tipo "MAV", "DALLAS" quando já existe memória.
    if memoria_livro:
        nomes_no_texto = extrair_nomes_provaveis(capitulo or "")
        for nome in nomes_no_texto:
            g, conf = genero_personagem(memoria_livro, nome)
            if g == "M": score_m += conf
            if g == "F": score_f += conf

    if score_m >= score_f + 4 and score_m >= 6:
        return "M", score_m
    if score_f >= score_m + 4 and score_f >= 6:
        return "F", score_f
    return None, max(score_m, score_f)


def detectar_genero_pov_seguro(original, traduzido, memoria=None):
    score_m, score_f = _pontuar_genero_pov(original, traduzido)
    if memoria:
        score_m += memoria.get("pov_m", 0)
        score_f += memoria.get("pov_f", 0)
    if score_m >= score_f + 3 and score_m >= 4:
        return "M"
    if score_f >= score_m + 3 and score_f >= 4:
        return "F"
    return None


def _corrigir_em_janela_primeira_pessoa(texto, alvo, substituto, janela_chars=140):
    padrao = re.compile(rf"\b{re.escape(alvo)}\b", flags=re.I)
    partes = []
    ultimo = 0
    marcadores = re.compile(
        r"(?:\beu\b|\bme\b|\bmeu\b|\bminha\b|\bestou\b|\btô\b|\bto\b|\bfiquei\b|\bfico\b|"
        r"\bsou\b|\bestava\b|\btava\b|\bcontinuo\b|\bme\s+sinto\b|\bsinto-me\b|"
        r"\bme\s+sentia\b|\bme\s+vi\b|\bvou\s+ficar\b|\bposso\s+ficar\b|"
        r"\bdevo\s+ficar\b|\bcomecei\s+a\s+ficar\b|\bestou\s+ficando\b)",
        flags=re.I,
    )
    for m in padrao.finditer(texto):
        inicio = max(0, m.start() - janela_chars)
        janela = texto[inicio:m.start()]
        # Não atravessa muita pontuação: reduz risco em diálogos misturados.
        if len(re.findall(r"[.!?]", janela)) > 1:
            continue
        if marcadores.search(janela):
            partes.append(texto[ultimo:m.start()])
            partes.append(_match_case(substituto, m.group(0)))
            ultimo = m.end()
    partes.append(texto[ultimo:])
    return "".join(partes)


def corrigir_genero_primeira_pessoa(texto, genero_pov):
    if not texto or genero_pov not in ["M", "F"]:
        return texto
    novo = str(texto)
    for masc, fem in PARES_GENERO_ADJETIVOS:
        if genero_pov == "M":
            novo = _corrigir_em_janela_primeira_pessoa(novo, fem, masc, 150)
        else:
            novo = _corrigir_em_janela_primeira_pessoa(novo, masc, fem, 150)
    return novo


def corrigir_genero_primeira_pessoa_forte(texto, genero_pov, confianca=0, fantasia=False):
    if not texto or genero_pov not in ["M", "F"]:
        return texto
    limite = CONFIANCA_CORRIGIR_FANTASIA if fantasia else CONFIANCA_CORRIGIR_NORMAL
    if confianca < limite:
        return corrigir_genero_primeira_pessoa(texto, genero_pov)
    novo = str(texto)
    for masc, fem in PARES_GENERO_ADJETIVOS:
        if genero_pov == "M":
            novo = _corrigir_em_janela_primeira_pessoa(novo, fem, masc, 220)
        else:
            novo = _corrigir_em_janela_primeira_pessoa(novo, masc, fem, 220)
    return novo


def corrigir_genero_por_memoria_dialogo(original, texto, memoria, memoria_livro=None):
    if not texto:
        return texto
    memoria = memoria or {}
    novo = str(texto)
    original_l = str(original or "").lower()
    hist = memoria.get("historico", "")
    hist_longo = (hist + " " + novo)[-9000:].lower()
    hist_curto = (hist + " " + novo)[-2500:].lower()

    if _texto_indica_feminino(hist_longo) and not _tem_bloqueio_masculino_real(hist_curto):
        memoria["alvo_dialogo"] = "F"
        memoria["forca_alvo"] = min(18, memoria.get("forca_alvo", 0) + 3)
    elif _texto_indica_masculino(hist_longo) and not _tem_bloqueio_feminino_real(hist_curto):
        if not any(p in hist_curto for p in ["princesa", "filha", "menina", "garota", "pequena"]):
            memoria["alvo_dialogo"] = "M"
            memoria["forca_alvo"] = min(18, memoria.get("forca_alvo", 0) + 3)

    alvo = memoria.get("alvo_dialogo")
    forca = memoria.get("forca_alvo", 0)
    fantasia = bool(memoria.get("fantasia") or (memoria_livro or {}).get("fantasia"))
    limite = 7 if fantasia else 4

    original_tem_apelido_neutro = re.search(APELIDOS_NEUTROS_ORIGINAL, original_l, flags=re.I)
    original_tem_feminino = any(re.search(p, original_l, flags=re.I) for p in RELACOES_FEM_ORIGINAL)
    original_tem_masculino = any(re.search(p, original_l, flags=re.I) for p in RELACOES_MASC_ORIGINAL)

    # Se o original aponta para nome conhecido no livro, usa o gênero aprendido.
    genero_nome = None
    conf_nome = 0
    if memoria_livro:
        for nome in extrair_nomes_provaveis(original):
            g, conf = genero_personagem(memoria_livro, nome)
            if conf > conf_nome:
                genero_nome, conf_nome = g, conf

    if genero_nome and conf_nome >= 6:
        alvo = genero_nome
        forca = max(forca, min(14, conf_nome))

    # Feminino: kid/squirt/baby/honey quando histórico tem princess/daughter/girl.
    if alvo == "F" and forca >= limite and (original_tem_apelido_neutro or original_tem_feminino or re.search(r"\bgaroto\b|\bpequeno\b|\bquerido\b", novo, flags=re.I)):
        if not _tem_bloqueio_masculino_real(novo):
            novo = _trocar_palavra(novo, "garoto", "garota")
            novo = _trocar_palavra(novo, "pequeno", "pequena")
            novo = _trocar_palavra(novo, "querido", "querida")
            novo = _trocar_palavra(novo, "bonito", "bonita")
            novo = _trocar_palavra(novo, "lindo", "linda")
    elif alvo == "M" and forca >= limite and (original_tem_apelido_neutro or original_tem_masculino):
        if not _tem_bloqueio_feminino_real(novo):
            novo = _trocar_palavra(novo, "garota", "garoto")
            novo = _trocar_palavra(novo, "pequena", "pequeno")
            novo = _trocar_palavra(novo, "querida", "querido")
            novo = _trocar_palavra(novo, "bonita", "bonito")
            novo = _trocar_palavra(novo, "linda", "lindo")

    if _texto_indica_feminino(novo):
        memoria["alvo_dialogo"] = "F"
        memoria["forca_alvo"] = min(18, memoria.get("forca_alvo", 0) + 2)
    elif _texto_indica_masculino(novo) and not any(p in hist_curto for p in ["princesa", "filha", "menina", "garota"]):
        memoria["alvo_dialogo"] = "M"
        memoria["forca_alvo"] = min(18, memoria.get("forca_alvo", 0) + 2)

    memoria["historico"] = (hist + " " + novo)[-14000:]
    memoria["forca_alvo"] = max(0, memoria.get("forca_alvo", 0) - 1)
    return novo


def corrigir_dialogo_feminino_forte(original, texto, memoria, memoria_livro=None):
    if not texto:
        return texto
    novo = str(texto)
    original_l = str(original or "").lower()
    hist = str(memoria.get("historico", "") or "").lower()
    hist_livro = str((memoria_livro or {}).get("historico_livro", "") or "").lower()

    feminino_recente = any(p in hist[-6000:] for p in [
        "princesa", "filha", "menina", "garota", "pequena", "minha filha", "minha garota", "senhorita"
    ])
    feminino_livro = "fem_rel" in hist_livro[-12000:]
    masculino_real_agora = _tem_bloqueio_masculino_real(novo)
    apelido_neutro_original = re.search(APELIDOS_NEUTROS_ORIGINAL, original_l, flags=re.I)

    if (feminino_recente or feminino_livro) and not masculino_real_agora:
        if apelido_neutro_original or re.search(r"\bgaroto\b|\bpequeno\b|\bquerido\b", novo, flags=re.I):
            novo = _trocar_palavra(novo, "garoto", "garota")
            novo = _trocar_palavra(novo, "pequeno", "pequena")
            novo = _trocar_palavra(novo, "querido", "querida")
    return novo


def revisar_genero_sequencia(originais, traduzidas, memoria_livro=None, capitulo=""):
    """
    Revisão GLOBAL ordenada do capítulo/arquivo interno.
    VERSÃO 4:
    1. memória temporária do livro inteiro;
    2. dicionário automático de personagens;
    3. memória de POV por capítulo;
    4. modo narrador externo/autora;
    5. corretor forte de primeira pessoa;
    6. memória de relação;
    7. sistema de confiança conservador para fantasia/harém reverso.
    """
    if memoria_livro is None:
        memoria_livro = criar_memoria_livro()

    original_all = " ".join(str(x or "") for x in originais)
    traduzido_all = " ".join(str(x or "") for x in traduzidas)
    fantasia = detectar_cena_fantasia_ou_confusa(original_all, traduzido_all) or bool(memoria_livro.get("fantasia"))
    if fantasia:
        memoria_livro["fantasia"] = True

    pov_global, confianca_pov = detectar_pov_global_capitulo(originais, traduzidas, memoria_livro, capitulo)
    if pov_global in ["M", "F"]:
        memoria_livro.setdefault("pov_capitulos", {})[capitulo or f"cap_{len(memoria_livro.get('pov_capitulos', {}))+1}"] = pov_global
        memoria_livro["ultimo_pov"] = pov_global

    memoria = {
        "historico": "",
        "alvo_dialogo": None,
        "forca_alvo": 0,
        "pov_m": 35 if pov_global == "M" else 0,
        "pov_f": 35 if pov_global == "F" else 0,
        "pov_global": pov_global,
        "fantasia": fantasia,
    }

    saida = []

    for original, traduzido in zip(originais, traduzidas):
        texto = str(traduzido or "")
        original = str(original or "")

        aprender_personagens_do_trecho(original, texto, memoria_livro)

        score_m, score_f = _pontuar_genero_pov(original, texto)
        memoria["pov_m"] = min(60, memoria.get("pov_m", 0) + score_m)
        memoria["pov_f"] = min(60, memoria.get("pov_f", 0) + score_f)

        if pov_global == "EXTERNO":
            genero_pov = None
        else:
            genero_pov = pov_global or detectar_genero_pov_seguro(original, texto, memoria)

        # Correção segura/forte de primeira pessoa. Em fantasia exige confiança maior.
        texto = corrigir_genero_primeira_pessoa(texto, genero_pov)
        texto = corrigir_genero_primeira_pessoa_forte(texto, genero_pov, confianca=confianca_pov, fantasia=fantasia)

        # Diálogo/apelidos por memória e relação.
        texto = corrigir_genero_por_memoria_dialogo(original, texto, memoria, memoria_livro)
        texto = corrigir_dialogo_feminino_forte(original, texto, memoria, memoria_livro)
        texto = revisar_texto_final(texto)

        saida.append(texto)

        memoria["historico"] = (str(memoria.get("historico", "")) + " " + texto)[-16000:]
        memoria_livro["historico_livro"] = (str(memoria_livro.get("historico_livro", "")) + " " + texto)[-60000:]

        # Decaimento apenas quando não há POV global confiável.
        if not pov_global:
            memoria["pov_m"] = max(0, memoria.get("pov_m", 0) - 1)
            memoria["pov_f"] = max(0, memoria.get("pov_f", 0) - 1)

    return saida


def preparar_contexto_para_traducao(textos):
    """Junta parágrafos com separador do estilo Calibre."""
    limpos = []
    for t in textos:
        limpos.append(limpar_texto_pre_traducao(substituir_sites_por_marca(t)))
    return CALIBRE_SEPARATOR.join(limpos), limpos



def limpar_sites_soup(soup):
    """
    Remove propagandas/sites como OceanofPDF, z-library e links gigantes
    sem apagar o capítulo inteiro.
    """
    padrao_site = re.compile(
        r"(ocean\s*of\s*pdf|oceanofpdf|oceanpdf|z[\s\-_]*library|z[\s\-_]*lib|1lib|libgen|anna['’]?s[\s\-_]*archive|wattpad)",
        flags=re.I,
    )

    # Remove links que apontam para esses sites
    for tag in list(soup.find_all(["a", "span", "p", "div", "center", "font", "small", "i", "em", "b", "strong"])):
        txt = tag.get_text(" ", strip=True)
        attrs = " ".join(str(v) for v in tag.attrs.values())

        if padrao_site.search(attrs):
            tag.decompose()
            continue

        # Se a tag inteira é basicamente o site, remove a tag completa
        txt_limpo = re.sub(r"\s+", "", txt).lower()
        if txt and len(txt) <= 80 and padrao_site.search(txt_limpo):
            tag.decompose()
            continue

    # Limpa texto solto sem apagar o parágrafo todo
    for node in list(soup.find_all(string=True)):
        try:
            parent = getattr(node, "parent", None)
            parent_name = getattr(parent, "name", "") if parent else ""

            if parent_name in ["script", "style", "head", "meta", "link", "title"]:
                continue

            original = str(node)
            novo = substituir_sites_por_marca(original)

            if novo != original:
                if novo.strip():
                    node.replace_with(NavigableString(novo))
                else:
                    node.extract()
        except Exception:
            pass

    # Remove parágrafos/divs que ficaram vazios depois da limpeza
    for tag in list(soup.find_all(["p", "div", "span", "center", "font", "small"])):
        if not tag.get_text(" ", strip=True) and not tag.find(["img", "svg"]):
            tag.decompose()

    return soup


def texto_sujo(texto):
    if not texto:
        return True

    t_original = str(texto).strip()
    t = t_original.lower()
    compacto = re.sub(r"[\s\"'<>/\\:;,.()-]+", "", t)

    sujeiras = [
        "xml version", "encoding=", "<?xml",
        "<html", "xmlns", "doctype", "{{id_",
        "html public", "xhtml 1.1", "xhtml11.dtd",
        "w3.org/tr/xhtml11/dtd", "w3c//dtd",
    ]

    if t in ["html", "body", "head"]:
        return True

    for s in sujeiras:
        if s in t:
            return True

    if (
        "htmlpublic" in compacto
        or "w3cdtdxhtml" in compacto
        or "xhtml11dtd" in compacto
        or "w3orgtrxhtml11dtd" in compacto
    ):
        return True

    return False



def parece_nome_proprio_ou_lugar(texto):
    """
    Usado SOMENTE para não colocar nomes próprios/cidades/países/marcas no ponto de atenção.
    Não mexe na tradução.
    """
    if not texto:
        return False

    t = str(texto).strip()
    t = re.sub(r"[“”\"'.,;:!?()\[\]{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    if not t:
        return False

    palavras = t.split()

    if len(palavras) > 7:
        return False

    conectores = {
        "de", "da", "do", "dos", "das",
        "van", "von", "del", "di", "la", "le",
        "of", "the", "and", "y", "e"
    }

    comuns_inicio_frase = {
        "The", "And", "But", "For", "This", "That", "There", "Then", "When",
        "Where", "What", "Why", "How", "He", "She", "They", "We", "You", "I",
        "A", "An", "In", "On", "At", "To", "From", "With", "Without",
        "Chapter", "Prologue", "Epilogue", "Cover", "Dedication"
    }

    palavras_reais = [p for p in palavras if p.lower() not in conectores]
    if not palavras_reais:
        return False

    if len(palavras_reais) == 1 and palavras_reais[0] in comuns_inicio_frase:
        return False

    for p in palavras:
        pl = p.lower()

        if pl in conectores:
            continue

        if re.match(r"^[A-Z]{2,8}$", p):
            continue

        if re.match(r"^[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÀ-ÿ'’\-]{1,}$", p):
            continue

        if re.match(r"^[A-Z]\.?$", p):
            continue

        return False

    return True


def limpar_lixo_tecnico_bruto_html(html):
    """
    Remove somente lixo técnico de EPUB quebrado, antes do BeautifulSoup.
    Não altera texto de história e não altera a lógica de tradução.
    """
    if not html:
        return html

    html = re.sub(r"<\?xml[^>]*\?>", "", html, flags=re.I | re.S)
    html = re.sub(r"<!DOCTYPE[^>]*(?:>|$)", "", html, flags=re.I | re.S)

    # DTD solto como texto, inclusive com espaços/quebras estranhas.
    html = re.sub(
        r"(?is)\bhtml\s+PUBLIC\s*[\"']?\s*-\s*//\s*W3C\s*//\s*DTD\s+XHTML\s+1\.1\s*//\s*EN\s*[\"']?\s*[\"']?\s*https?\s*:\s*/\s*/\s*www\s*\.?\s*w3\s*\.?\s*org\s*/\s*TR\s*/\s*xhtml11\s*/\s*DTD\s*/\s*xhtml11\s*\.?\s*dtd\s*[\"']?",
        "",
        html,
    )

    html = re.sub(
        r"(?is)\bhtml\s+PUBLIC\s*[\"']?\s*-\s*//\s*W3C\s*//\s*DTD\s+XHTML\s+1\.1\s*//\s*EN\s*[\"']?",
        "",
        html,
    )

    return html


def limpar_lixo_tecnico_soup(soup):
    """
    Remove lixo técnico visível já dentro do BeautifulSoup.
    Não mexe em <style>, <head>, CSS real, classes ou layout.
    """
    for node in list(soup.find_all(string=True)):
        try:
            if isinstance(node, (Comment, Doctype, Declaration, ProcessingInstruction)):
                node.extract()
                continue

            texto = str(node)
            limpo = re.sub(r"\s+", " ", texto).strip()
            compacto = re.sub(r"[\s\"'<>/\\:;,.()-]+", "", limpo).lower()

            if not limpo:
                continue

            parent = node.parent
            parent_name = getattr(parent, "name", "") or ""

            if parent_name in ["style", "script", "head", "meta", "link", "title"]:
                continue

            if (
                "htmlpublic" in compacto
                or "w3cdtdxhtml" in compacto
                or "xhtml11dtd" in compacto
                or "w3orgtrxhtml11dtd" in compacto
                or re.search(r"\bhtml\s+PUBLIC\b", limpo, flags=re.I)
                or re.search(r"\bXHTML\s+1\.1\b", limpo, flags=re.I)
                or re.search(r"\bxhtml11\s*\.?\s*dtd\b", limpo, flags=re.I)
            ):
                node.extract()
                continue

            if limpo.lower() == "cover":
                node.replace_with("Capa")

        except Exception:
            pass

    return soup

def resumo_erros(erros, limite=5):
    if not erros:
        return ""

    partes = ["\n\n⚠️ Pontos com atenção:"]

    for erro in erros[-limite:]:
        partes.append(
            f"\n📁 Arquivo: {erro.get('arquivo', 'não identificado')}\n"
            f"📖 Capítulo: {erro.get('capitulo', 'Capítulo não identificado')}\n"
            f"🧩 Trecho: {erro.get('texto', 'não identificado')}\n"
            f"📝 Motivo: {erro.get('motivo', 'não informado')}"
        )

    if len(erros) > limite:
        partes.append(f"\n… +{len(erros) - limite} ponto(s) no log.")

    return "\n".join(partes)


def request_url(url, data=None, headers=None, method="GET"):
    headers = headers or {}

    if method == "GET":
        if data:
            url += "?" + urllib.parse.urlencode(data)
        req = urllib.request.Request(url, headers=headers, method="GET")
    else:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="ignore")


def google_new_translate(texto):
    url = "https://translate-pa.googleapis.com/v1/translate"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/133.0.0.0 Safari/537.36",
    }
    data = {
        "params.client": "gtx",
        "query.source_language": "en",
        "query.target_language": "pt",
        "query.display_language": "pt-BR",
        "data_types": "TRANSLATION",
        "key": "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA",
        "query.text": texto,
    }
    resposta = request_url(url, data=data, headers=headers, method="GET")
    return json.loads(resposta)["translation"]


def google_html_translate(texto):
    url = "https://translate-pa.googleapis.com/v1/translateHtml"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json+protobuf",
        "X-Goog-Api-Key": "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/133.0.0.0 Safari/537.36",
    }
    body = json.dumps([[[texto], "en", "pt"], "wt_lib"])
    resposta = request_url(url, data=body, headers=headers, method="POST")
    return json.loads(resposta)[0][0]


def google_old_translate(texto):
    url = "https://translate.googleapis.com/translate_a/single"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/111.0.0.0 Safari/537.36",
    }
    data = {
        "client": "gtx",
        "sl": "en",
        "tl": "pt",
        "dt": "t",
        "dj": 1,
        "q": texto,
    }
    method = "GET" if len(texto) <= 1800 else "POST"
    resposta = request_url(url, data=data, headers=headers, method=method)
    dados = json.loads(resposta)
    return "".join(item["trans"] for item in dados["sentences"])


def traduzir_google(texto, mecanismo):
    if mecanismo == "google_html":
        return google_html_translate(texto)
    if mecanismo == "google_old":
        return google_old_translate(texto)
    return google_new_translate(texto)


def ordem_fallback(mecanismo_principal):
    todos = ["google_new", "google_html", "google_old"]
    ordem = [mecanismo_principal] + [m for m in todos if m != mecanismo_principal]
    return ordem


def traduzir_com_retry(texto, mecanismo):
    ultimo_erro = None
    texto = substituir_sites_por_marca(texto)

    for mecanismo_teste in ordem_fallback(mecanismo):
        for tentativa in range(REQUEST_ATTEMPTS):
            try:
                traducao = traduzir_google(texto, mecanismo_teste)

                if traducao and traducao.strip() and traducao.strip() != texto.strip():
                    return revisar_texto_final(traducao), None

                ultimo_erro = f"{nome_mecanismo(mecanismo_teste)} voltou igual ao original"

            except Exception as erro:
                ultimo_erro = f"{nome_mecanismo(mecanismo_teste)}: {str(erro)[:100]}"

            if tentativa < REQUEST_ATTEMPTS - 1:
                time.sleep(2 + tentativa * 2)

    return revisar_texto_final(texto), ultimo_erro or "falha desconhecida"


def traduzir_com_fallback(texto, mecanismo):
    traducao, erro = traduzir_com_retry(texto, mecanismo)

    if not erro:
        return traducao, None

    partes = [texto]
    traduzidas = []
    falhas = 0

    for parte in partes:
        if not parte.strip():
            continue

        t, e = traduzir_com_retry(parte, mecanismo)

        if e:
            traduzidas.append(revisar_texto_final(parte))
            falhas += 1
        else:
            traduzidas.append(t)

    if falhas:
        return " ".join(traduzidas), f"{falhas} frase(s) ficaram sem tradução"

    return " ".join(traduzidas), None


def criar_sep(i):
    return SEP_TEMPLATE.format(format(i, "05"))


def separar_por_sep(texto, quantidade):
    """
    Alinhamento parecido com o plugin do Calibre:
    ele junta trechos usando duas quebras de linha e depois separa de volta.
    """
    if quantidade <= 1:
        return [revisar_texto_final(texto)] if texto and texto.strip() else []

    partes = [p.strip() for p in re.split(r"\n\s*\n+", texto) if p.strip()]

    if len(partes) == quantidade:
        return partes

    if len(partes) > quantidade:
        return partes[:quantidade - 1] + ["\n\n".join(partes[quantidade - 1:])]

    return partes
def precisa_alerta_revisao(original, texto_final):
    """Evita ponto de atenção falso. Só avisa quando sobrou inglês/site/lixo."""
    if not texto_final or texto_sujo(texto_final):
        return False

    if re.search(r"oceanofpdf|z-library|1lib|z-lib|ocean\s*pdf", texto_final, flags=re.I):
        return True

    if re.search(r"[a-záàâãéêíóôõúç]{3,}[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}", texto_final):
        return True

    palavras_ingles = re.findall(
        r"\b(the|and|with|your|you|she|he|they|this|that|was|were|have|from|into|would|could|should|because|before|after|when|where|what)\b",
        texto_final,
        flags=re.I,
    )
    if len(palavras_ingles) >= 2 and not parece_nome_proprio_ou_lugar(texto_final):
        return True

    if "armas" in detectar_contexto_original(original):
        if re.search(r"\b(chute|chutes|pontapé|pontapés)\b", texto_final, flags=re.I):
            return True

    if texto_final.strip().lower() == str(original).strip().lower():
        if re.search(r"\b(the|and|with|you|your|she|he|they|was|were|from)\b", original, flags=re.I):
            return True

    return False


def detectar_trechos_nao_traduzidos(original, texto_final):
    """
    Detecta frases/palavras que provavelmente ficaram em inglês depois da tradução.
    Retorna uma lista curta para colocar no log final.
    """
    if not texto_final:
        return []

    texto = str(texto_final)

    padroes = [
        r"\b(the|and|with|your|you|she|he|they|this|that|was|were|have|from|into|would|could|should|because|before|after|when|where|what|why|how|then|there|here)\b",
        r"\b(I|I'm|I've|I'll|I'd|don't|didn't|can't|couldn't|shouldn't|won't|wasn't|weren't|isn't|aren't)\b",
    ]

    encontrados = []

    for p in padroes:
        for m in re.finditer(p, texto, flags=re.I):
            trecho = m.group(0).strip()
            if trecho and trecho.lower() not in [e.lower() for e in encontrados]:
                encontrados.append(trecho)

    # Pega mini-frases ainda em inglês, mas evita nomes próprios curtos.
    possiveis = re.findall(
        r"\b(?:[A-Z]?[a-z]+(?:'t|'m|'re|'ve|'ll|'d)?\s+){1,5}(?:the|and|with|your|you|she|he|they|was|were|from|because|before|after)\b(?:\s+[A-Za-z']+){0,4}",
        texto,
        flags=re.I,
    )

    for trecho in possiveis:
        trecho = re.sub(r"\s+", " ", trecho).strip()
        if len(trecho) > 4 and trecho.lower() not in [e.lower() for e in encontrados]:
            if not parece_nome_proprio_ou_lugar(trecho):
                encontrados.append(trecho)

    return encontrados[:8]


def criar_erro_revisao(bloco_id, idx, original, texto_final, motivo_extra=None):
    nao_traduzidos = detectar_trechos_nao_traduzidos(original, texto_final)

    motivo = motivo_extra or "precisa de revisão"
    if nao_traduzidos:
        motivo = "trecho/palavra possivelmente não traduzido"

    return {
        "bloco": bloco_id,
        "trecho_num": idx,
        "motivo": motivo,
        "original": texto_curto(original, 700),
        "traducao": texto_curto(texto_final, 700),
        "nao_traduzidos": ", ".join(nao_traduzidos) if nao_traduzidos else "",
        "texto": texto_curto(texto_final, 420),
    }


def traduzir_bloco_sync(item):
    bloco_id, textos, mecanismo = item

    junto, textos_limpos = preparar_contexto_para_traducao(textos)
    traducao, erro = traduzir_com_retry(junto, mecanismo)

    if not erro:
        partes = separar_por_sep(traducao, len(textos_limpos))

        if len(partes) == len(textos_limpos):
            partes = [
                corrigir_coerencia_contextual(original, revisar_texto_final(parte))
                for original, parte in zip(textos_limpos, partes)
            ]
            return bloco_id, partes, []

    partes_finais = []
    erros = []

    for idx, texto in enumerate(textos_limpos, start=1):
        if texto_sujo(texto):
            partes_finais.append(texto)
            continue

        t, e = traduzir_com_fallback(texto, mecanismo)
        texto_final = corrigir_coerencia_contextual(texto, revisar_texto_final(t))
        partes_finais.append(texto_final)

        if e and precisa_alerta_revisao(texto, texto_final):
            erros.append(criar_erro_revisao(
                bloco_id,
                idx,
                texto,
                texto_final,
                "sobrou inglês/site/lixo técnico ou tradução incoerente"
            ))

    return bloco_id, partes_finais, erros



def texto_visivel(node):
    if not isinstance(node, NavigableString):
        return False

    if isinstance(node, (Comment, Doctype, Declaration, ProcessingInstruction)):
        return False

    texto = str(node).strip()

    if not texto:
        return False

    parent = node.parent

    if not parent:
        return False

    if parent.name in ["[document]", "script", "style", "code", "pre", "head", "meta", "link", "title"]:
        return False

    if texto_sujo(texto):
        return False

    if not re.search(r"[A-Za-z]", texto):
        return False

    return True


def parece_capitulo(texto):
    if not texto:
        return False

    t = re.sub(r"\s+", " ", str(texto)).strip().lower()

    padroes = [
        r"^chapter\s+",
        r"^cap[ií]tulo\s+",
        r"^prologue$",
        r"^pr[oó]logo$",
        r"^epilogue$",
        r"^ep[ií]logo$",
    ]

    return any(re.search(p, t) for p in padroes)


def contexto_capitulo(soup, arquivo_nome=""):
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "title"]):
        txt = tag.get_text(" ", strip=True)
        if txt and parece_capitulo(txt):
            return texto_curto(txt, 120)

    nome = Path(str(arquivo_nome)).stem
    nome_limpo = nome.replace("_", " ").replace("-", " ")
    nome_limpo = re.sub(r"\s+", " ", nome_limpo).strip()

    if parece_capitulo(nome_limpo):
        return texto_curto(nome_limpo, 120)

    return "Capítulo não identificado"


def montar_blocos(nos):
    blocos = []
    bloco = []
    tamanho = 0

    for item in nos:
        _, _, texto = item
        extra = len(texto) + 30

        if bloco and tamanho + extra > MERGE_LENGTH:
            blocos.append(bloco)
            bloco = []
            tamanho = 0

        bloco.append(item)
        tamanho += extra

    if bloco:
        blocos.append(bloco)

    return blocos


async def traduzir_blocos(blocos, mecanismo, workers):
    loop = asyncio.get_running_loop()
    tarefas_textos = []

    for bloco_id, bloco in enumerate(blocos, start=1):
        textos = [texto for _, _, texto in bloco]
        tarefas_textos.append((bloco_id, textos, mecanismo))

    resultados = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tarefas = [
            loop.run_in_executor(executor, traduzir_bloco_sync, item)
            for item in tarefas_textos
        ]

        for tarefa in asyncio.as_completed(tarefas):
            resultados.append(await tarefa)

    return resultados



def limpar_texto_pre_traducao(texto):
    """
    Limpeza ANTES da tradução, para imitar melhor o comportamento do Calibre:
    remove hifenização falsa e espaços quebrados antes de enviar ao Google.
    """
    if not texto:
        return texto

    texto = str(texto)
    texto = texto.replace("\u00ad", "")
    texto = texto.replace("‐", "-").replace("‑", "-")

    texto = re.sub(
        r"([A-Za-zÀ-ÿ]{2,})-\s+([a-záàâãéêíóôõúç]{2,})",
        r"\1\2",
        texto
    )

    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def bloco_traduzivel(tag):
    if not tag or not getattr(tag, "name", None):
        return False

    if tag.name in ["script", "style", "code", "pre", "head", "meta", "link", "title"]:
        return False

    if tag.find(["img", "svg", "math"]):
        return False

    if tag.find(["p", "div", "li", "blockquote", "h1", "h2", "h3", "h4"]):
        return False

    texto = tag.get_text(" ", strip=True)

    if not texto or len(texto.strip()) < 2:
        return False

    if texto_sujo(texto):
        return False

    if not re.search(r"[A-Za-z]", texto):
        return False

    return True


def coletar_blocos_texto(soup):
    """
    Extração no estilo do Ebook Translator do Calibre.
    Prioriza parágrafos e blocos reais, não spans soltos.
    Isso evita perder contexto e reduz erro tipo shot -> chute.
    """
    tags_prioritarias = [
        "p", "pre", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "td", "th", "caption",
    ]
    tags_blocos = set(tags_prioritarias + ["div", "section", "article", "main"])
    blocos = []
    contador = 1

    def tem_bloco_filho(tag):
        for filho in tag.find_all(tags_blocos):
            if filho is not tag:
                return True
        return False

    candidatos = soup.find_all(tags_prioritarias + ["div"])

    for tag in candidatos:
        if not getattr(tag, "name", None):
            continue

        if tag.name in ["script", "style", "code", "head", "meta", "link", "title"]:
            continue

        if tag.name == "div" and tem_bloco_filho(tag):
            continue

        texto_bruto = tag.get_text(" ", strip=True)
        if not texto_bruto or len(texto_bruto.strip()) < 2:
            continue

        if texto_sujo(texto_bruto):
            continue

        if not re.search(r"[A-Za-z]", texto_bruto):
            continue

        texto = limpar_texto_pre_traducao(texto_bruto)
        texto = substituir_sites_por_marca(texto)

        if not texto or len(texto.strip()) < 2:
            continue

        blocos.append((contador, tag, texto))
        contador += 1

    if blocos:
        return blocos

    for node in soup.find_all(string=True):
        if not texto_visivel(node):
            continue

        texto = limpar_texto_pre_traducao(str(node))
        texto = substituir_sites_por_marca(texto)

        if len(texto.strip()) < 2:
            continue

        blocos.append((contador, node, texto))
        contador += 1

    return blocos
def substituir_texto_no_item(item_html, texto_final):
    """Mantém a tag e atributos, trocando só o conteúdo textual do bloco."""
    if hasattr(item_html, "clear") and hasattr(item_html, "append"):
        attrs = dict(getattr(item_html, "attrs", {}) or {})
        item_html.clear()
        for k, v in attrs.items():
            item_html[k] = v
        item_html.append(NavigableString(texto_final))
    else:
        item_html.replace_with(NavigableString(texto_final))



async def traduzir_html(html, mecanismo, arquivo_nome="", memoria_livro=None):
    html = limpar_lixo_tecnico_bruto_html(html)
    soup = BeautifulSoup(html, "html.parser")
    soup = limpar_sites_soup(soup)
    soup = limpar_lixo_tecnico_soup(soup)
    capitulo = contexto_capitulo(soup, arquivo_nome)

    # NOVA LÓGICA DE TESTE:
    # Traduz por blocos visuais inteiros, parecido com o Calibre.
    # Isso evita traduzir pedaços soltos de spans e reduz palavras coladas.
    nos = coletar_blocos_texto(soup)

    if not nos:
        return str(soup), 0, []

    blocos = montar_blocos(nos)
    workers = CONFIGS.get(mecanismo, CONFIGS["google_new"])["workers"]
    resultados = await traduzir_blocos(blocos, mecanismo, workers)

    mapa_blocos = {i: bloco for i, bloco in enumerate(blocos, start=1)}

    erros = []
    alterados = 0

    # VERSÃO 2: primeiro junta todos os trechos traduzidos em ordem,
    # depois aplica a memória longa no capítulo/arquivo inteiro.
    todos_itens = []
    todos_originais = []
    todos_traduzidos = []

    for bloco_id, partes_traduzidas, erros_bloco in sorted(resultados, key=lambda x: x[0]):
        bloco = mapa_blocos.get(bloco_id, [])

        if len(partes_traduzidas) != len(bloco):
            primeiro_texto = texto_curto(bloco[0][2] if bloco else "Trecho não identificado")
            erros.append({
                "capitulo": capitulo,
                "bloco": bloco_id,
                "trecho_num": "?",
                "motivo": "desalinhamento de partes",
                "texto": substituir_sites_por_marca(primeiro_texto),
            })
            continue

        for item, texto_traduzido in zip(bloco, partes_traduzidas):
            _, node_or_tag, original = item
            todos_itens.append(item)
            todos_originais.append(original)
            todos_traduzidos.append(texto_traduzido)

        for erro in erros_bloco:
            if texto_sujo(erro.get("texto", "")):
                continue

            if not parece_nome_proprio_ou_lugar(erro.get("texto", "")):
                erro["capitulo"] = capitulo
                erros.append(erro)

    todos_traduzidos = revisar_genero_sequencia(todos_originais, todos_traduzidos, memoria_livro=memoria_livro, capitulo=capitulo)

    for item, texto_traduzido in zip(todos_itens, todos_traduzidos):
        _, node_or_tag, original = item

        if texto_traduzido and texto_traduzido.strip():
            texto_final = revisar_texto_final(texto_traduzido)
            substituir_texto_no_item(node_or_tag, texto_final)

            if texto_final.strip() != original.strip():
                alterados += 1

    soup = limpar_sites_soup(soup)
    soup = limpar_lixo_tecnico_soup(soup)
    html_final = str(soup)
    return html_final, alterados, erros



def aplicar_css_calibre_like(book):
    """
    Ajuste visual leve para aproximar o EPUB do resultado do Calibre,
    sem mexer na tradução e sem apagar estilos originais.
    """
    css = """
    html, body {
        margin-left: 5% !important;
        margin-right: 5% !important;
        padding: 0 !important;
        line-height: 1.35 !important;
    }

    h1, h2, h3, h4 {
        text-align: center !important;
        margin-top: 10% !important;
        margin-bottom: 1.2em !important;
        font-weight: bold !important;
    }

    h1 + p, h2 + p, h3 + p,
    .subtitle, .sub-title, .author, .byline {
        text-align: center !important;
    }

    p {
        margin-top: 0.55em !important;
        margin-bottom: 0.55em !important;
    }

    i, em {
        font-style: italic !important;
    }

    blockquote, .quote, .epigraph, .dedication {
        font-style: italic !important;
        margin-left: 7% !important;
        margin-right: 7% !important;
    }

    /* Possíveis mensagens de celular. Só aplica se o EPUB tiver classes com esses nomes. */
    [class*="sms"], [class*="text"], [class*="message"],
    [class*="chat"], [class*="bubble"], [class*="phone"],
    [class*="msg"], [class*="imessage"] {
        display: block !important;
        width: fit-content !important;
        max-width: 82% !important;
        margin-top: 0.25em !important;
        margin-bottom: 0.25em !important;
        padding: 0.28em 0.7em !important;
        border-radius: 0.9em !important;
        background: #eeeeee !important;
        color: #111111 !important;
        text-align: left !important;
        font-style: normal !important;
    }
    """

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            style_tag = soup.new_tag("style")
            style_tag.string = css

            if soup.head:
                soup.head.append(style_tag)
            else:
                soup.insert(0, style_tag)

            item.set_content(str(soup).encode("utf-8"))

        except Exception:
            pass

def aplicar_estetica_celular_e_capitulo(book):
    css = """
    body {
        margin-left: 5% !important;
        margin-right: 5% !important;
        line-height: 1.35 !important;
    }

    h1, h2, h3, h4 {
        text-align: center !important;
        margin-top: 12% !important;
        margin-bottom: 1em !important;
    }

    .alma-capitulo {
        text-align: center !important;
        font-weight: bold !important;
        margin-top: 12% !important;
        margin-bottom: 0.4em !important;
        border-bottom: 1px solid #aaa !important;
        width: fit-content !important;
        margin-left: auto !important;
        margin-right: auto !important;
        padding-bottom: 0.25em !important;
    }

    .alma-nome-capitulo {
        text-align: center !important;
        font-style: italic !important;
        font-weight: bold !important;
        font-size: 1.25em !important;
        margin-bottom: 4em !important;
    }

    .alma-sms {
        display: block !important;
        width: fit-content !important;
        max-width: 78% !important;
        background: #eeeeee !important;
        color: #222 !important;
        border-radius: 999px !important;
        padding: 0.28em 0.75em !important;
        margin: 0.25em 0 0.25em 7% !important;
        text-align: left !important;
        font-style: normal !important;
        font-size: 0.92em !important;
        line-height: 1.15 !important;
    }
    """

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            style_tag = soup.new_tag("style")
            style_tag.string = css

            if soup.head:
                soup.head.append(style_tag)
            else:
                soup.insert(0, style_tag)

            textos = soup.find_all(["p", "div", "h1", "h2", "h3"])

            for i, tag in enumerate(textos):
                txt = tag.get_text(" ", strip=True)

                if re.search(r"cap[ií]tulo\s+\d+|pr[oó]logo|ep[ií]logo", txt, re.I):
                    tag["class"] = tag.get("class", []) + ["alma-capitulo"]

                    if i + 1 < len(textos):
                        prox = textos[i + 1]
                        prox_txt = prox.get_text(" ", strip=True)
                        if prox_txt and len(prox_txt) <= 40:
                            prox["class"] = prox.get("class", []) + ["alma-nome-capitulo"]

                if 2 <= len(txt) <= 95:
                    antes = textos[i - 1].get_text(" ", strip=True) if i > 0 else ""
                    depois = textos[i + 1].get_text(" ", strip=True) if i + 1 < len(textos) else ""

                    if (
                        len(antes) <= 95
                        and len(depois) <= 120
                        and not re.search(r"cap[ií]tulo|pr[oó]logo|ep[ií]logo", txt, re.I)
                    ):
                        tag["class"] = tag.get("class", []) + ["alma-sms"]

            item.set_content(str(soup).encode("utf-8"))

        except Exception:
            pass
            

def criar_pagina_marca():
    pagina = epub.EpubHtml(
        title="Alma Scriptum",
        file_name="alma_scriptum.xhtml",
        lang="pt-BR",
    )

    imagem_html = ""

    if MARCA_IMAGEM.exists():
        imagem_html = """
        <div style="text-align:center; margin:0; padding:0;">
            <img src="images/alma_scriptum.png"
            style="width:100%; max-width:900px; display:block; margin:0 auto;">
        </div>
        """

    pagina.content = f"""
    <html>
    <body style="margin:0; padding:0; background:#ffffff;">
        {imagem_html}
    </body>
    </html>
    """

    return pagina


def adicionar_pagina_marca(book):
    pagina = criar_pagina_marca()

    if MARCA_IMAGEM.exists():
        imagem = epub.EpubItem(
            uid="alma_img",
            file_name="images/alma_scriptum.png",
            media_type="image/png",
            content=MARCA_IMAGEM.read_bytes(),
        )
        book.add_item(imagem)

    book.add_item(pagina)

    spine = list(book.spine)

    if len(spine) > 1:
        spine.insert(1, pagina)
    else:
        spine.append(pagina)

    book.spine = spine


def extrair_capa_epub(caminho_epub):
    try:
        book = epub.read_epub(str(caminho_epub))
        imagens = list(book.get_items_of_type(ITEM_IMAGE))

        if not imagens:
            return None

        escolhida = None

        for img in imagens:
            nome = (img.file_name or "").lower()
            if "cover" in nome or "capa" in nome:
                escolhida = img
                break

        if escolhida is None:
            escolhida = imagens[0]

        ext = ".jpg"
        media = getattr(escolhida, "media_type", "") or ""

        if "png" in media:
            ext = ".png"
        elif "webp" in media:
            ext = ".webp"

        caminho = TEMP_DIR / f"capa_{uuid.uuid4().hex}{ext}"

        with open(caminho, "wb") as f:
            f.write(escolhida.get_content())

        return caminho

    except Exception:
        return None


async def atualizar_progresso(mensagem, mecanismo, i, total, erros):
    if not mensagem:
        return

    try:
        porcentagem = int((i / total) * 100)
        barra = barra_progresso(porcentagem)

        await mensagem.edit_text(
            f"📚 Alma Scriptum Translate\n\n"
            f"⚙️ Mecanismo: {nome_mecanismo(mecanismo)}\n"
            f"📖 Arquivo interno: {i}/{total}\n"
            f"📊 Progresso: {porcentagem}%\n\n"
            f"{barra}\n\n"
            f"✨ Traduzindo... aguarde.\n"
            f"🧾 Registro: {len(erros)} ponto(s) encontrado(s).\n"
            f"📄 O TXT final vai trazer somente palavras/frases possivelmente não traduzidas."
        )
    except Exception:
        pass


def salvar_log(nome_base, erros):
    if not erros:
        return None

    apenas_nao_traduzidos = [
        erro for erro in erros
        if erro.get("nao_traduzidos")
    ]

    if not apenas_nao_traduzidos:
        return None

    caminho = TEMP_DIR / f"palavras_nao_traduzidas_{uuid.uuid4().hex}.txt"

    with open(caminho, "w", encoding="utf-8") as f:
        f.write("PALAVRAS / FRASES POSSIVELMENTE NÃO TRADUZIDAS — ALMA SCRIPTUM\n")
        f.write(f"Livro: {nome_base}\n")
        f.write("=" * 70 + "\n\n")

        for i, erro in enumerate(apenas_nao_traduzidos, start=1):
            f.write(f"PONTO {i}\n")
            f.write("-" * 70 + "\n")
            f.write(f"Arquivo interno: {erro.get('arquivo', 'não identificado')}\n")
            f.write(f"Capítulo: {erro.get('capitulo', 'Capítulo não identificado')}\n")
            f.write(f"Palavra/frase possível: {erro.get('nao_traduzidos', '')}\n")

            if erro.get("original"):
                f.write("\nOriginal:\n")
                f.write(str(erro.get("original", "")).strip() + "\n")

            if erro.get("traducao"):
                f.write("\nTradução:\n")
                f.write(str(erro.get("traducao", "")).strip() + "\n")

            f.write("\n" + "=" * 70 + "\n\n")

    return caminho


def _normalizar_zip_path(caminho):
    return str(caminho).replace("\\", "/").lstrip("/")


def _encontrar_opf_no_epub(arquivos):
    container_path = "META-INF/container.xml"

    if container_path not in arquivos:
        return None

    try:
        soup = BeautifulSoup(arquivos[container_path].decode("utf-8", errors="ignore"), "xml")
        rootfile = soup.find("rootfile")
        if rootfile and rootfile.get("full-path"):
            return _normalizar_zip_path(rootfile.get("full-path"))
    except Exception:
        return None

    return None


def atualizar_titulo_epub_zip(arquivos, titulo_final):
    opf_path = _encontrar_opf_no_epub(arquivos)

    if not opf_path or opf_path not in arquivos:
        return arquivos

    try:
        soup = BeautifulSoup(arquivos[opf_path].decode("utf-8", errors="ignore"), "xml")
        titulo_limpo = Path(titulo_final).stem

        title_tag = soup.find("dc:title")
        if title_tag:
            title_tag.string = titulo_limpo
        else:
            metadata = soup.find("metadata")
            if metadata:
                novo_title = soup.new_tag("dc:title")
                novo_title.string = titulo_limpo
                metadata.append(novo_title)

        arquivos[opf_path] = str(soup).encode("utf-8")

    except Exception as erro:
        print(f"⚠️ Não consegui renomear título interno: {erro}")

    return arquivos


def adicionar_pagina_marca_zip(arquivos):
    if not MARCA_IMAGEM.exists():
        return arquivos

    opf_path = _encontrar_opf_no_epub(arquivos)

    if not opf_path or opf_path not in arquivos:
        return arquivos

    try:
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""

        pagina_rel = "alma_scriptum.xhtml"
        imagem_rel = "images/alma_scriptum.png"

        pagina_path = _normalizar_zip_path(f"{opf_dir}/{pagina_rel}" if opf_dir else pagina_rel)
        imagem_path = _normalizar_zip_path(f"{opf_dir}/{imagem_rel}" if opf_dir else imagem_rel)

        pagina_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>Alma Scriptum</title></head>\n'
            '<body style="margin:0; padding:0; background:#ffffff;">\n'
            '<div style="text-align:center; margin:0; padding:0;">\n'
            f'<img src="{imagem_rel}" alt="Alma Scriptum" '
            'style="width:100%; max-width:900px; display:block; margin:0 auto;" />\n'
            '</div>\n'
            '</body>\n'
            '</html>\n'
        )

        soup = BeautifulSoup(arquivos[opf_path].decode("utf-8", errors="ignore"), "xml")
        manifest = soup.find("manifest")
        spine = soup.find("spine")

        if manifest:
            for old in manifest.find_all("item"):
                if old.get("id") in ["alma_scriptum_page", "alma_scriptum_img"]:
                    old.decompose()

            item_img = soup.new_tag("item")
            item_img["id"] = "alma_scriptum_img"
            item_img["href"] = imagem_rel
            item_img["media-type"] = "image/png"
            manifest.append(item_img)

            item_page = soup.new_tag("item")
            item_page["id"] = "alma_scriptum_page"
            item_page["href"] = pagina_rel
            item_page["media-type"] = "application/xhtml+xml"
            manifest.append(item_page)

        if spine:
            for old in spine.find_all("itemref"):
                if old.get("idref") == "alma_scriptum_page":
                    old.decompose()

            itemref = soup.new_tag("itemref")
            itemref["idref"] = "alma_scriptum_page"

            refs = spine.find_all("itemref")

            # coloca a página da marca depois do primeiro item do spine
            # para não quebrar a abertura/capa original do EPUB
            if refs:
                refs[0].insert_after(itemref)
            else:
                spine.append(itemref)

        arquivos[opf_path] = str(soup).encode("utf-8")
        arquivos[pagina_path] = pagina_html.encode("utf-8")
        arquivos[imagem_path] = MARCA_IMAGEM.read_bytes()

    except Exception as erro:
        print(f"⚠️ Não consegui adicionar a marca no EPUB preservado: {erro}")

    return arquivos


async def traduzir_epub(entrada, saida, mecanismo, user_id, mensagem=None, adicionar_marca=True, nome_original=None):
    erros = []
    traduzidos = 0

    extensoes_html = (".xhtml", ".html", ".htm")

    with zipfile.ZipFile(str(entrada), "r") as zip_in:
        nomes_originais = zip_in.namelist()
        arquivos = {nome: zip_in.read(nome) for nome in nomes_originais}

    documentos = [
        nome for nome in nomes_originais
        if nome.lower().endswith(extensoes_html)
        and not nome.lower().endswith("nav.xhtml")
    ]

    total = len(documentos) or 1

    # Memória temporária do livro inteiro: nasce aqui e morre quando termina o EPUB.
    memoria_livro = criar_memoria_livro()

    for i, nome in enumerate(documentos, start=1):
        if user_id in cancelamentos:
            raise Exception("Tradução cancelada.")

        try:
            conteudo = arquivos[nome].decode("utf-8", errors="ignore")

            if conteudo.strip():
                traduzido, alterados, erros_html = await traduzir_html(
                    conteudo,
                    mecanismo,
                    nome,
                    memoria_livro=memoria_livro,
                )

                for erro in erros_html:
                    erro["arquivo"] = f"{i}/{total}"
                    erros.append(erro)

                if traduzido and traduzido != conteudo:
                    arquivos[nome] = traduzido.encode("utf-8")

                if alterados > 0:
                    traduzidos += 1

                print(f"✅ Traduzido {i}/{total}: {nome}")

        except Exception as erro:
            erros.append({
                "arquivo": f"{i}/{total}",
                "capitulo": "Capítulo não identificado",
                "bloco": "-",
                "trecho_num": "-",
                "motivo": str(erro)[:120],
                "texto": "erro geral no arquivo interno",
            })
            print(f"⚠️ Arquivo interno {i}/{total}: {str(erro)[:120]}")

        await atualizar_progresso(mensagem, mecanismo, i, total, erros)
        await asyncio.sleep(REQUEST_INTERVAL)

    if traduzidos == 0:
        raise Exception("Nenhum texto foi traduzido. Teste outro EPUB ou outro modo Google.")

    nome_base_para_titulo = nome_original or Path(entrada).name
    titulo_final = criar_nome_final(nome_base_para_titulo)
    for _nome_arq in list(arquivos.keys()):
        if _nome_arq.lower().endswith((".opf", ".ncx", ".xml")):
            try:
                _txt = arquivos[_nome_arq].decode("utf-8", errors="ignore")
                _txt = substituir_sites_por_marca(_txt)
                arquivos[_nome_arq] = _txt.encode("utf-8")
            except Exception:
                pass

    arquivos = atualizar_titulo_epub_zip(arquivos, titulo_final)

    if adicionar_marca:
        try:
            arquivos = adicionar_pagina_marca_zip(arquivos)
            print("✅ Página Alma Scriptum adicionada.")
        except Exception as erro:
            print("⚠️ Erro ao adicionar marca:", erro)

    nomes_finais = list(nomes_originais)
    for nome in arquivos:
        if nome not in nomes_finais:
            nomes_finais.append(nome)

    with zipfile.ZipFile(str(saida), "w", compression=zipfile.ZIP_DEFLATED) as zip_out:
        for nome in nomes_finais:
            zip_out.writestr(nome, arquivos[nome])

    return erros


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not usuario_liberado(user_id):
        await update.message.reply_text("⛔ Você não possui acesso ao Alma Scriptum Translate.")
        return

    if user_id not in usuarios:
        usuarios[user_id] = {"marca": False, "mecanismo": "google_new"}

    mecanismo = usuarios[user_id]["mecanismo"]
    marca = "✅ Ativada" if usuarios[user_id]["marca"] else "❌ Desativada"

    await update.message.reply_text(
        "📚 Alma Scriptum Translate\n\n"
        "✨ Modo organizado\n"
        "⚡ Mantém estrutura do EPUB\n🧹 Limpeza de sites ativada\n"
        "📖 EPUB Inglês → Português\n\n"
        f"⚙️ Mecanismo atual: {nome_mecanismo(mecanismo)}\n"
        f"🖼️ Marca: {marca}\n\n"
        "📤 Escolha o mecanismo e envie um EPUB.",
        reply_markup=teclado_principal(),
    )


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    user_id = query.from_user.id

    if not usuario_liberado(user_id):
        await query.message.reply_text("⛔ Acesso negado.")
        return

    if user_id not in usuarios:
        usuarios[user_id] = {"marca": False, "mecanismo": "google_new"}

    if query.data == "set_google_new":
        usuarios[user_id]["mecanismo"] = "google_new"

    elif query.data == "set_google_html":
        usuarios[user_id]["mecanismo"] = "google_html"

    elif query.data == "set_google_old":
        usuarios[user_id]["mecanismo"] = "google_old"

    elif query.data == "marca":
        usuarios[user_id]["marca"] = not usuarios[user_id]["marca"]

    elif query.data == "cancelar":
        cancelamentos.add(user_id)
        await query.message.reply_text("🛑 Cancelamento solicitado. O bot vai parar no próximo arquivo interno.")
        return

    mecanismo = usuarios[user_id]["mecanismo"]
    marca = "✅ Ativada" if usuarios[user_id]["marca"] else "❌ Desativada"

    await query.message.reply_text(
        "📚 Alma Scriptum Translate\n\n"
        "✨ Modo organizado\n"
        "⚡ Mantém estrutura do EPUB\n🧹 Limpeza de sites ativada\n"
        "📖 EPUB Inglês → Português\n\n"
        f"⚙️ Mecanismo atual: {nome_mecanismo(mecanismo)}\n"
        f"🖼️ Marca: {marca}\n\n"
        "📤 Agora envie o EPUB.",
        reply_markup=teclado_principal(),
    )


async def receber_arquivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not usuario_liberado(user_id):
        await update.message.reply_text("⛔ Você não possui acesso.")
        return

    cancelamentos.discard(user_id)

    if user_id not in usuarios:
        usuarios[user_id] = {"marca": False, "mecanismo": "google_new"}

    documento = update.message.document

    if not documento.file_name.lower().endswith(".epub"):
        await update.message.reply_text("⚠️ Envie apenas arquivo EPUB.")
        return

    nome = limpar_nome(documento.file_name)

    entrada = TEMP_DIR / f"{uuid.uuid4()}_{nome}"
    saida = TEMP_DIR / f"PTBR_{nome}"

    mecanismo = usuarios[user_id]["mecanismo"]

    mensagem = await update.message.reply_text(
        "📚 EPUB recebido\n\n"
        f"⚙️ Mecanismo principal: {nome_mecanismo(mecanismo)}\n"
        "⚡ Tradução com fallback automático iniciada..."
    )

    arquivo = await documento.get_file()
    await arquivo.download_to_drive(str(entrada))

    log_path = None
    capa_path = None
    saida_final = None

    try:
        capa_path = extrair_capa_epub(entrada)

        erros = await traduzir_epub(
            entrada=entrada,
            saida=saida,
            mecanismo=mecanismo,
            user_id=user_id,
            mensagem=mensagem,
            adicionar_marca=usuarios[user_id]["marca"],
            nome_original=documento.file_name,
        )

        try:
            await mensagem.edit_text("📦 Enviando EPUB traduzido...")
        except Exception:
            pass

        nome_final = criar_nome_final(documento.file_name)
        saida_final = TEMP_DIR / nome_final

        try:
            saida.rename(saida_final)
        except Exception:
            saida_final = saida

        print("📌 Nome final enviado:", nome_final)

        if capa_path and capa_path.exists():
            try:
                with open(capa_path, "rb") as capa_file:
                    await update.message.reply_photo(
                        photo=capa_file,
                    )

            except Exception:
                pass

        with open(saida_final, "rb") as arquivo_saida:
            try:
                arquivo_telegram = InputFile(arquivo_saida, filename=nome_final)

                await update.message.reply_document(
                    document=arquivo_telegram,
                    caption="✨ Tradução concluída por Alma Scriptum Translate\n📝 Se houver palavras/frases não traduzidas, envio o TXT logo abaixo.",
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=120,
                    pool_timeout=120,
                )

            except (NetworkError, TimedOut):
                await update.message.reply_text(
                    "⚠️ O EPUB foi traduzido, mas o Telegram falhou ao enviar."
                )


        if erros:
            log_path = salvar_log(nome_final, erros)


            if log_path and log_path.exists():
                with open(log_path, "rb") as log_file:
                    await update.message.reply_document(
                        document=log_file,
                        filename="Palavras não traduzidas ✦ Alma Scriptum.txt",
                        caption="📝 TXT enviado junto com o EPUB: somente palavras/frases possivelmente não traduzidas.",
                    )
        else:
            await update.message.reply_text("✅ EPUB entregue sem erros detectados.")

    except Exception as erro:
        await update.message.reply_text(f"❌ {erro}")

    finally:
        try:
            entrada.unlink(missing_ok=True)
            saida.unlink(missing_ok=True)

            if saida_final:
                saida_final.unlink(missing_ok=True)

            if log_path:
                log_path.unlink(missing_ok=True)

            if capa_path:
                capa_path.unlink(missing_ok=True)

        except Exception:
            pass


async def erro_global(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Erro capturado: {context.error}")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(120)
        .pool_timeout(120)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_arquivo))
    app.add_error_handler(erro_global)

    print("✅ Alma Scriptum Translate CONTEXTUAL ONLINE!")

    app.run_polling()


if __name__ == "__main__":
    main()
