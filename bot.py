import os
import re
import uuid
import json
import time
import html as html_lib
import asyncio
import urllib.parse
import urllib.request
from pathlib import Path
from bs4 import NavigableString
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup, NavigableString
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
}

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

MARCA_IMAGEM = BASE_DIR / "alma_scriptum.png"

usuarios = {}
cancelamentos = set()

MERGE_LENGTH = 1200
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

    padroes = [
        r"OceanofPDF\.com",
        r"OceanOfPDF\.com",
        r"OceanPDF\.com",
        r"oceanofpdf\.com",
        r"oceanofpdf",
        r"OceanofPDF",
        r"Ocean Of PDF",
        r"Ocean PDF",
        r"z-library\.sk",
        r"z-library",
        r"zlib",
        r"1lib\.sk",
        r"1lib",
        r"z-lib\.org",
        r"z-lib",
    ]

    for p in padroes:
        texto = re.sub(p, "", texto, flags=re.IGNORECASE)

    texto = re.sub(r"\s{2,}", " ", texto)
    return texto.strip()


def revisar_texto_final(texto):
    if not texto:
        return texto

    texto = html_lib.unescape(texto)
    texto = substituir_sites_por_marca(texto)

    texto = texto.replace("&quot;", '"')
    texto = texto.replace("&#39;", "'")
    texto = texto.replace("&amp;", "&")

    texto = re.sub(r"([a-záàâãéêíóôõúç])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])", r"\1 \2", texto)

    correcoes = {
        "deTODOS": "de TODOS",
        "de TODOS.Cada": "de TODOS. Cada",
        "TODOS.Cada": "TODOS. Cada",
        "Cada persona": "Cada persona",
        "paraa": "para a",
        "para aNo": "para a No",
        "para aNa": "para a Na",
        "passaEm": "passa em",
        "se passaEm": "se passa em",
        "completaincluindo": "completa incluindo",
        "incluindoo": "incluindo o",
        "incluindoa": "incluindo a",
        "deda": "de da",
        "dea": "de a",
        "doa": "do a",
        "daA": "da A",
        "doO": "do O",
        "noA": "no A",
        "naA": "na A",
        "emA": "em A",
        "deA": "de A",
    }

    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)

    texto = re.sub(r"\b([a-záàâãéêíóôõúç]{3,})([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})\b", r"\1 \2", texto)
    texto = re.sub(r"\s+([,.!?;:])", r"\1", texto)
    texto = re.sub(r"([,.!?;:])([A-Za-zÀ-ÿ])", r"\1 \2", texto)
    texto = re.sub(r"\s+", " ", texto)

    return texto.strip()


def texto_sujo(texto):
    if not texto:
        return True

    t = str(texto).lower().strip()

    sujeiras = [
        "xml version", "encoding=", "utf-8", "<?xml",
        "<html", "xmlns", "doctype", "{{id_",
    ]

    if t in ["html", "body", "head"]:
        return True

    for s in sujeiras:
        if s in t:
            return True

    return False


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

    partes = re.split(r"(?<=[.!?])\s+", texto)
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
    partes = [texto]

    for i in range(quantidade - 1):
        pattern = r"\{\{\s*id\s*_\s*" + format(i, "05") + r"\s*\}\}"
        novo = []

        for parte in partes:
            novo.extend(re.split(pattern, parte, maxsplit=1))

        partes = novo

    partes = [
        re.sub(r"\{\{\s*id\s*_\s*\d+\s*\}\}", "", p).strip()
        for p in partes
    ]

    return [p for p in partes if p]


def traduzir_bloco_sync(item):
    bloco_id, textos, mecanismo = item

    textos = [substituir_sites_por_marca(t) for t in textos]

    junto = ""

    for i, texto in enumerate(textos):
        junto += texto

        if i < len(textos) - 1:
            junto += "\n\n" + criar_sep(i) + "\n\n"

    traducao, erro = traduzir_com_retry(junto, mecanismo)

    if not erro:
        partes = separar_por_sep(traducao, len(textos))

        if len(partes) == len(textos):
            partes = [revisar_texto_final(p) for p in partes]
            return bloco_id, partes, []

    partes_finais = []
    erros = []

    for idx, texto in enumerate(textos, start=1):

        if texto_sujo(texto):
            partes_finais.append(texto)
            continue

        t, e = traduzir_com_fallback(texto, mecanismo)

        texto_final = revisar_texto_final(t)

        partes_finais.append(texto_final)

        # Só adiciona ponto de atenção
        # se REALMENTE sobrou problema
        if e:

            ainda_tem_ingles = bool(re.search(
                r"\b(the|and|with|you|your|she|he|they|this|that|was|were|have|from|into|would|could|should)\b",
                texto_final,
                flags=re.IGNORECASE
            ))

            palavra_grudada = bool(re.search(
                r"[a-záàâãéêíóôõúç]{3,}[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,}",
                texto_final
            ))

            site_sobrou = bool(re.search(
                r"oceanofpdf|z-library|1lib|z-lib",
                texto_final,
                flags=re.IGNORECASE
            ))

            texto_igual_original = (
                texto_final.strip().lower() ==
                texto.strip().lower()
            )

            if (
                ainda_tem_ingles
                or palavra_grudada
                or site_sobrou
                or texto_igual_original
            ):

                erros.append({
                    "bloco": bloco_id,
                    "trecho_num": idx,
                    "motivo": "precisa de revisão manual",
                    "texto": texto_curto(texto_final),
                })

    return bloco_id, partes_finais, erros


def texto_visivel(node):
    if not isinstance(node, NavigableString):
        return False

    texto = str(node).strip()

    if not texto:
        return False

    parent = node.parent

    if not parent:
        return False

    if parent.name in ["script", "style", "code", "pre", "head", "meta", "link", "title"]:
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

async def traduzir_html(html, mecanismo, arquivo_nome="", user_id=None):
    soup = BeautifulSoup(html, "html.parser")
    capitulo = contexto_capitulo(soup, arquivo_nome)

    nos = []
    contador = 1

    for node in soup.find_all(string=True):
        if not texto_visivel(node):
            continue

        texto = str(node)

        if len(texto.strip()) < 2:
            continue

        nos.append((contador, node, texto))
        contador += 1

    if not nos:
        return str(soup), 0, []

    blocos = montar_blocos(nos)
    workers = CONFIGS.get(mecanismo, CONFIGS["google_new"])["workers"]
    resultados = await traduzir_blocos(blocos, mecanismo, workers)

    mapa_blocos = {i: bloco for i, bloco in enumerate(blocos, start=1)}

    erros = []
    alterados = 0

    for bloco_id, partes_traduzidas, erros_bloco in resultados:

        if user_id in cancelamentos:
            raise Exception("Tradução cancelada.")

        bloco = mapa_blocos.get(bloco_id, [])

        if len(partes_traduzidas) != len(bloco):
            primeiro_texto = texto_curto(
                bloco[0][2] if bloco else "Trecho não identificado"
            )

            erros.append({
                "capitulo": capitulo,
                "bloco": bloco_id,
                "trecho_num": "?",
                "motivo": "desalinhamento de partes",
                "texto": substituir_sites_por_marca(primeiro_texto),
            })
            continue

        for item, texto_traduzido in zip(bloco, partes_traduzidas):
            _, node, original = item

            if texto_traduzido and texto_traduzido.strip():
                texto_final = revisar_texto_final(texto_traduzido)
                node.replace_with(NavigableString(texto_final))

                if texto_final.strip() != original.strip():
                    alterados += 1

        for erro in erros_bloco:
            if texto_sujo(erro.get("texto", "")):
                continue

            erro["capitulo"] = capitulo
            erros.append(erro)


    html_final = str(soup)
    html_final = html_lib.unescape(html_final)
    html_final = substituir_sites_por_marca(html_final)

    html_final = re.sub(r"([a-záàâãéêíóôõúç])(<[^/][^>]*>)([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])", r"\1 \2\3", html_final)
    html_final = re.sub(r"([a-záàâãéêíóôõúç])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][a-záàâãéêíóôõúç]{2,})", r"\1 \2", html_final)
    html_final = re.sub(r"([.!?])([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])", r"\1 \2", html_final)

    html_final = html_final.replace("deTODOS", "de TODOS")
    html_final = html_final.replace("TODOS.Cada", "TODOS. Cada")
    html_final = html_final.replace("completaincluindo", "completa incluindo")
    html_final = html_final.replace("paraaNo", "para a No")
    html_final = html_final.replace("passaEm", "passa em")
    html_final = html_final.replace("eununcadeixarei", "eu nunca deixarei")

    return html_final, alterados, erros
        

def aplicar_css_calibre_like(book):
    css = """
    body {
        margin-left: 6% !important;
        margin-right: 6% !important;
        line-height: 1.35 !important;
    }

    h1, h2, h3, h4 {
        text-align: center !important;
        margin-top: 18% !important;
        margin-bottom: 1.5em !important;
    }

    h1 + p, h2 + p, h3 + p {
        text-align: center !important;
        font-style: italic !important;
        font-weight: bold !important;
    }

    p {
        margin-top: 0.75em !important;
        margin-bottom: 0.75em !important;
    }

    i, em {
        font-style: italic !important;
    }

    [class*="sms"], [class*="text"], [class*="message"],
    [class*="chat"], [class*="bubble"], [class*="phone"] {
        display: block !important;
        max-width: 80% !important;
        width: fit-content !important;
        margin: 0.35em auto 0.35em 8% !important;
        padding: 0.35em 0.75em !important;
        border-radius: 1em !important;
        background: #e8e8e8 !important;
        color: #111 !important;
        text-align: left !important;
        font-style: normal !important;
    }
    """

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")

            if soup.head:
                style_tag = soup.new_tag("style")
                style_tag.string = css
                soup.head.append(style_tag)
            else:
                style_tag = soup.new_tag("style")
                style_tag.string = css
                soup.insert(0, style_tag)

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
        bloco_erros = resumo_erros(erros, limite=3)

        await mensagem.edit_text(
            f"📚 Alma Scriptum Translate\n\n"
            f"⚙️ Mecanismo: {nome_mecanismo(mecanismo)}\n"
            f"📖 Arquivo interno: {i}/{total}\n"
            f"📊 Progresso: {porcentagem}%\n\n"
            f"{barra}\n\n"
            f"✨ Traduzindo... aguarde."
            f"{bloco_erros}"
        )
    except Exception:
        pass


def salvar_log(nome_base, erros):
    if not erros:
        return None

    caminho = TEMP_DIR / f"log_erros_{uuid.uuid4().hex}.txt"

    with open(caminho, "w", encoding="utf-8") as f:
        f.write(f"LOG DE PONTOS COM ATENÇÃO — {nome_base}\n")
        f.write("=" * 70 + "\n\n")

        for i, erro in enumerate(erros, start=1):
            f.write(f"PONTO {i}\n")
            f.write(f"Arquivo: {erro.get('arquivo', 'não identificado')}\n")
            f.write(f"Capítulo: {erro.get('capitulo', 'Capítulo não identificado')}\n")
            f.write(f"Bloco: {erro.get('bloco', 'não informado')}\n")
            f.write(f"Trecho Nº: {erro.get('trecho_num', 'não informado')}\n")
            f.write(f"Motivo: {erro.get('motivo', 'não informado')}\n")
            f.write(f"Trecho exato:\n{erro.get('texto', 'não identificado')}\n")
            f.write("\n" + "-" * 70 + "\n\n")

    return caminho


async def traduzir_epub(entrada, saida, mecanismo, user_id, mensagem=None, adicionar_marca=True):
    book = epub.read_epub(str(entrada))
    documentos = list(book.get_items_of_type(ITEM_DOCUMENT))
    total = len(documentos)

    traduzidos = 0
    erros = []

    for i, item in enumerate(documentos, start=1):
        if user_id in cancelamentos:
            raise Exception("Tradução cancelada.")

        try:
            conteudo = item.get_content().decode("utf-8", errors="ignore")

            if not conteudo.strip():
                await atualizar_progresso(mensagem, mecanismo, i, total, erros)
                continue

            arquivo_nome = getattr(item, "file_name", f"arquivo_{i}")
            traduzido, alterados, erros_html = await traduzir_html(
              conteudo,
              mecanismo,
              arquivo_nome,
              user_id,
         )

            for erro in erros_html:
                erro["arquivo"] = f"{i}/{total}"
                erros.append(erro)

            if alterados > 0:
                traduzidos += 1

            item.set_content(traduzido.encode("utf-8"))

            print(f"✅ Traduzido {i}/{total}")

        except Exception as erro:
            erro_txt = {
                "arquivo": f"{i}/{total}",
                "capitulo": "Capítulo não identificado",
                "bloco": "-",
                "trecho_num": "-",
                "motivo": str(erro)[:120],
                "texto": "erro geral no arquivo interno",
            }
            erros.append(erro_txt)
            print(f"⚠️ Arquivo interno {i}/{total}: {str(erro)[:120]}")

        await atualizar_progresso(mensagem, mecanismo, i, total, erros)
        await asyncio.sleep(REQUEST_INTERVAL)

    if traduzidos == 0:
        raise Exception("Nenhum texto foi traduzido. Teste outro EPUB ou outro modo Google.")
        

for item in book.get_items_of_type(ITEM_DOCUMENT):

    try:
        html = item.get_content().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.find_all(True):
            classes = tag.get("class")

            if classes:
                print("CLASSE ENCONTRADA:", classes)

    except Exception:
        pass
        

    aplicar_css_calibre_like(book)
    
    if adicionar_marca:
        adicionar_pagina_marca(book)

    epub.write_epub(str(saida), book)

    return erros 

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not usuario_liberado(user_id):
        await update.message.reply_text("⛔ Você não possui acesso ao Alma Scriptum Translate.")
        return

    if user_id not in usuarios:
        usuarios[user_id] = {"marca": True, "mecanismo": "google_new"}

    mecanismo = usuarios[user_id]["mecanismo"]
    marca = "✅ Ativada" if usuarios[user_id]["marca"] else "❌ Desativada"

    await update.message.reply_text(
        "📚 Alma Scriptum Translate\n\n"
        "✨ Modo organizado\n"
        "⚡ Mantém estrutura do EPUB\n"
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
        usuarios[user_id] = {"marca": True, "mecanismo": "google_new"}

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
        "⚡ Mantém estrutura do EPUB\n"
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
        usuarios[user_id] = {"marca": True, "mecanismo": "google_new"}

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
                    caption="✨ Tradução concluída por Alma Scriptum Translate",
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=90,
                    pool_timeout=90,
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
                        filename="Pontos de atenção ✦ Alma Scriptum.txt",
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
        .read_timeout(180)
        .write_timeout(180)
        .connect_timeout(90)
        .pool_timeout(90)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_arquivo))
    app.add_error_handler(erro_global)

    print("✅ Alma Scriptum Translate FALLBACK ONLINE!")

    app.run_polling()


if __name__ == "__main__":
    main()
