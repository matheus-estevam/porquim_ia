"""
Porquim Pessoal — bot de finanças no Telegram
================================================
Registra gastos por mensagem ("gasolina 150", "mercado 127,50 no nubank")
e gera relatórios por categoria. Tudo local: dados num SQLite no seu disco.

Como rodar:
    pip install python-telegram-bot
    # cole seu token (do @BotFather) em TOKEN abaixo
    python bot.py

Comandos no Telegram:
    (qualquer mensagem)  -> registra um gasto
    /relatorio           -> resumo do mês atual por categoria
    /resumo              -> total geral
    /ajuda               -> como usar
"""

import logging
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ======================= CONFIGURAÇÃO =======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    sys.exit("Erro: defina TELEGRAM_TOKEN no arquivo .env")
DB_PATH = Path(__file__).parent / "financas.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")   # Postgres (Neon) em produção; SQLite local se vazio
if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.strip().strip('"').strip("'")  # remove aspas/espaços coladas por engano
USA_PG = bool(DATABASE_URL)
PH = "%s" if USA_PG else "?"               # placeholder do driver (Postgres usa %s, SQLite usa ?)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ======================= CATEGORIAS =======================
# Palavra-chave -> categoria. Ajuste à sua realidade.
CATEGORIAS = {
    "Alimentação": ["mercado", "comida", "lanche", "misto", "almoço", "almoco",
                    "jantar", "ifood", "restaurante", "padaria", "açougue",
                    "pizza", "hamburguer", "hamburger", "café", "cafe", "feira"],
    "Transporte":  ["gasolina", "combustível", "combustivel", "uber", "99",
                    "ônibus", "onibus", "metrô", "metro", "estacionamento",
                    "pedágio", "pedagio", "etanol", "alcool"],
    "Moradia":     ["aluguel", "luz", "água", "agua", "internet", "condomínio",
                    "condominio", "gás", "gas", "energia"],
    "Pets":        ["ração", "racao", "veterinário", "veterinario", "pet"],
    "Saúde":       ["farmácia", "farmacia", "remédio", "remedio", "médico",
                    "medico", "academia", "dentista"],
    "Lazer":       ["cinema", "netflix", "spotify", "jogo", "bar", "cerveja",
                    "show", "viagem"],
    "Compras":     ["roupa", "tênis", "tenis", "shopping", "presente", "amazon", "mercadolivre", "mercado livre", "eletrônico", "eletronico", "shopee"],
    "Investimentos": ["investimento", "ação", "ações", "acoes", "acao", "fundo",
                      "tesouro", "cdb", "lci", "lca", "poupança", "poupanca",
                      "cripto", "bitcoin", "btc", "aporte", "previdência",
                      "previdencia"],
}

# Meios de pagamento reconhecidos no texto.
MEIOS = ["nubank", "itaú", "itau", "inter", "pix", "dinheiro", "crédito",
         "credito", "débito", "debito", "caixa", "bradesco", "santander",
         "cora", "picpay"]

EMOJI_CAT = {
    "Alimentação": "🍔", "Transporte": "🚗", "Moradia": "🏠", "Pets": "🐾",
    "Saúde": "💊", "Lazer": "🎮", "Compras": "🛍️", "Investimentos": "📈", "Outros": "📦",
}


# ======================= BANCO DE DADOS =======================
def conectar():
    """Abre uma conexão: Postgres (Neon) em produção, SQLite localmente."""
    if USA_PG:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    id_col = "SERIAL PRIMARY KEY" if USA_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    con = conectar()
    cur = con.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS gastos (
            id        {id_col},
            user_id   BIGINT,
            data      TEXT,
            descricao TEXT,
            categoria TEXT,
            meio      TEXT,
            valor     REAL,
            codigo    TEXT,
            criado_em TEXT
        )
    """)
    con.commit()
    con.close()


def salvar_gasto(user_id, descricao, categoria, meio, valor) -> str:
    codigo = uuid.uuid4().hex[:6]
    agora = datetime.now()
    con = conectar()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO gastos (user_id, data, descricao, categoria, meio, valor, codigo, criado_em) "
        f"VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH})",
        (user_id, agora.strftime("%d/%m/%Y"), descricao, categoria, meio,
         valor, codigo, agora.isoformat()),
    )
    con.commit()
    con.close()
    return codigo


def relatorio_mes(user_id):
    mes = datetime.now().strftime("%m/%Y")
    con = conectar()
    cur = con.cursor()
    cur.execute(
        "SELECT categoria, SUM(valor) FROM gastos "
        f"WHERE user_id={PH} AND substr(data,4) = {PH} GROUP BY categoria ORDER BY 2 DESC",
        (user_id, mes),
    )
    linhas = cur.fetchall()
    con.close()
    return linhas


def total_geral(user_id):
    con = conectar()
    cur = con.cursor()
    cur.execute(f"SELECT SUM(valor) FROM gastos WHERE user_id={PH}", (user_id,))
    total = cur.fetchone()[0] or 0
    con.close()
    return total


# ======================= PARSER (regex) =======================
def extrair_valor(texto: str):
    """Pega o maior número da mensagem como valor (heurística simples)."""
    achados = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+(?:\.\d+)?", texto)
    valores = []
    for a in achados:
        if "," in a:                       # formato BR: 1.234,56 ou 19,90
            a = a.replace(".", "").replace(",", ".")
        valores.append(float(a))
    return max(valores) if valores else None


def detectar(texto: str, mapa) -> str | None:
    t = texto.lower()
    for chave, palavras in (mapa.items() if isinstance(mapa, dict) else [(m, [m]) for m in mapa]):
        for p in palavras:
            if p in t:
                return chave
    return None


def parse_regex(texto: str) -> dict | None:
    valor = extrair_valor(texto)
    if valor is None:
        return None
    categoria = detectar(texto, CATEGORIAS) or "Outros"
    meio = detectar(texto, MEIOS) or "Não informado"
    # descrição = texto sem o número e sem ruído comum
    desc = re.sub(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+(?:\.\d+)?", "", texto)
    desc = re.sub(r"\b(no|na|com|de|do|da|r\$|reais?|cart[ãa]o)\b", "", desc, flags=re.I)
    desc = " ".join(desc.split()).strip(" ,.-").capitalize() or categoria
    return {"valor": valor, "categoria": categoria, "meio": meio.title(), "descricao": desc}


def parse_com_gemini(texto: str) -> dict | None:
    """Usa Gemini para categorizar. Cai no regex se a API falhar ou não estiver configurada."""
    if not GEMINI_API_KEY:
        return parse_regex(texto)
    import json
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = (
        "Extraia informações de gasto da mensagem e responda APENAS em JSON válido, sem texto extra:\n"
        '{"valor": float, "categoria": str, "meio": str, "descricao": str}\n'
        f"Categorias possíveis: {list(CATEGORIAS) + ['Outros']}.\n"
        f"Meios possíveis: {MEIOS + ['Não informado']}.\n"
        f"Mensagem: {texto}"
    )
    try:
        resposta = model.generate_content(prompt)
        txt = re.sub(r"```(?:json)?\n?", "", resposta.text).strip("`").strip()
        return json.loads(txt)
    except Exception as e:
        log.warning("Gemini falhou (%s), usando categorias locais.", e)
        return parse_regex(texto)


def parse(texto: str) -> dict | None:
    return parse_com_gemini(texto)


# ======================= HANDLERS =======================
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐷 *Porquim Pessoal* ativado!\n\n"
        "Me manda seus gastos do jeito que vier:\n"
        "• `gasolina 150`\n"
        "• `mercado 127,50 no nubank`\n"
        "• `misto quente 19`\n\n"
        "Comandos: /relatorio  /resumo  /ajuda",
        parse_mode="Markdown",
    )


async def ajuda(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "É só me dizer o que gastou: *descrição + valor* (e o cartão, se quiser).\n"
        "Eu identifico a categoria e guardo tudo.\n\n"
        "/relatorio — gastos do mês por categoria\n"
        "/resumo — total geral registrado",
        parse_mode="Markdown",
    )


async def registrar(update: Update, _: ContextTypes.DEFAULT_TYPE):
    dados = parse(update.message.text)
    if not dados or not dados.get("valor"):
        await update.message.reply_text(
            "🤔 Não achei um valor nessa mensagem. Tenta tipo: `mercado 50`",
            parse_mode="Markdown",
        )
        return
    codigo = salvar_gasto(
        update.effective_user.id, dados["descricao"], dados["categoria"],
        dados["meio"], dados["valor"],
    )
    emoji = EMOJI_CAT.get(dados["categoria"], "📦")
    await update.message.reply_text(
        f"✅ *Gasto Registrado!*\n"
        f"{emoji} {dados['descricao']} ({dados['categoria']})\n"
        f"💰 R$ {dados['valor']:.2f}".replace(".", ",") + "\n"
        f"💳 {dados['meio']}\n"
        f"📅 {datetime.now():%d/%m/%Y} — #{codigo}",
        parse_mode="Markdown",
    )


async def relatorio(update: Update, _: ContextTypes.DEFAULT_TYPE):
    linhas = relatorio_mes(update.effective_user.id)
    if not linhas:
        await update.message.reply_text("Nenhum gasto registrado neste mês ainda. 🐷")
        return
    total = sum(v for _, v in linhas)
    corpo = "\n".join(
        f"{EMOJI_CAT.get(cat, '📦')} {cat} → R$ {val:.2f}".replace(".", ",")
        for cat, val in linhas
    )
    await update.message.reply_text(
        f"📊 *Relatório de {datetime.now():%m/%Y}*\n\n{corpo}\n\n"
        f"💵 *Total: R$ {total:.2f}*".replace(".", ","),
        parse_mode="Markdown",
    )


async def resumo(update: Update, _: ContextTypes.DEFAULT_TYPE):
    total = total_geral(update.effective_user.id)
    await update.message.reply_text(
        f"💵 Total geral registrado: *R$ {total:.2f}*".replace(".", ","),
        parse_mode="Markdown",
    )


# ======================= KEEP-ALIVE (Render) =======================
class _HealthHandler(BaseHTTPRequestHandler):
    """Responde 200 OK para o health-check do Render e o ping do UptimeRobot."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Porquim Pessoal vivo! \xf0\x9f\x90\xb7")

    def log_message(self, *args):  # silencia o log de cada ping
        pass


def iniciar_keepalive():
    """Sobe um mini-servidor HTTP numa thread separada (porta exigida pelo Render)."""
    porta = int(os.getenv("PORT", "8080"))
    servidor = HTTPServer(("0.0.0.0", porta), _HealthHandler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    log.info("Keep-alive ouvindo na porta %s", porta)


def main():
    init_db()
    iniciar_keepalive()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("relatorio", relatorio))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar))
    log.info("Bot rodando. Ctrl+C para parar.")
    app.run_polling()


if __name__ == "__main__":
    main()
