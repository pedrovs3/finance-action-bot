from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import threading
import investpy
import yfinance as yf
import pandas as pd
import boto3
import os
import json
import requests
from datetime import datetime, timedelta
import schedule
import time
import logging
from openpyxl import load_workbook
from dotenv import load_dotenv
from colorama import Fore, Style
from requests.exceptions import HTTPError

# ==== Configuração Inicial ====
load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "sa-east-1")
EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE", "mailer@pedrovs.dev")
EMAIL_DESTINATARIOS = os.getenv("EMAIL_DESTINATARIOS", "").split(",")

if not EMAIL_DESTINATARIOS or EMAIL_DESTINATARIOS == [""]:
    raise ValueError("Nenhum destinatário configurado no ENV (EMAIL_DESTINATARIOS)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

RECOMMENDATION_DICT = dict(
    buy="Compra",
    strong_buy="Forte compra",
    hold="Mantenha",
    underperform="Desempenho inferior",
    strong_sell="Forte venda",
    sell="Venda"
)

CACHE_DIR = "cache"
CACHE_TTL = timedelta(hours=12)
os.makedirs(CACHE_DIR, exist_ok=True)

def carregar_cache(ticker):
    path = os.path.join(CACHE_DIR, f"{ticker}.json")
    if not os.path.exists(path):
        return None
    mod_time = datetime.fromtimestamp(os.path.getmtime(path))
    if datetime.now() - mod_time > CACHE_TTL:
        return None
    with open(path, "r") as f:
        return json.load(f)

def salvar_cache(ticker, info):
    path = os.path.join(CACHE_DIR, f"{ticker}.json")
    with open(path, "w") as f:
        json.dump(info, f)

def obter_lista_acoes_e_fiis():
    try:
        logger.info("Buscando lista de ativos via investpy...")
        ativos_br = investpy.get_stocks(country="brazil")
        ativos_br["symbol"] = ativos_br["symbol"].str.strip()

        fiis = ativos_br[ativos_br["symbol"].str.endswith("11")]
        acoes = ativos_br[~ativos_br["symbol"].str.endswith("11")]

        tickers_acoes = (acoes["symbol"] + ".SA").tolist()
        tickers_fiis = (fiis["symbol"] + ".SA").tolist()

        logger.info(f"Ações encontradas: {len(tickers_acoes)} | FIIs encontrados: {len(tickers_fiis)}")
        return tickers_acoes, tickers_fiis
    except Exception as e:
        logger.error(f"Erro ao buscar ativos: {e}")
        return [], []

def processar_acao(ticker, mercado, progresso, total, tipo_ativo="acao"):
    try:
        info = carregar_cache(ticker)
        if not info:
            acao = yf.Ticker(ticker)
            info = acao.info
            if not info or "symbol" not in info:
                logger.warning(f"Ticker inválido ou não encontrado: {ticker}")
                return None
            salvar_cache(ticker, info)
        else:
            logger.info(f"Usando cache para {ticker}")

        preco_atual = info.get("currentPrice", None)
        dividend_yield = info.get("dividendYield", 0) * 100
        setor = info.get("sector", "N/A")
        tipo = "FII" if tipo_ativo == "fii" else "Ação"

        if tipo_ativo == "fii":
            if not preco_atual or dividend_yield <= 0:
                return None
            retorno_anual = (dividend_yield / 100) * preco_atual

            chance_sucesso = 0
            chance_sucesso += min(dividend_yield, 15) * 3
            chance_sucesso += 10 if setor not in ["N/A", None, ""] else 0
            chance_sucesso += 20 if dividend_yield > 6 else 0
            chance_sucesso = min(max(chance_sucesso, 0), 100)

            percentual = (progresso + 1) / total * 100
            print(f"[{progresso + 1}/{total} - {percentual:.2f}%] Analisando FII {ticker}")

            return {
                "Ticker": ticker,
                "Mercado": mercado,
                "Tipo de Ativo": tipo,
                "Setor": setor,
                "Preço Atual (R$ ou US$)": round(preco_atual, 2),
                "Dividend Yield (%)": round(dividend_yield, 2),
                "Retorno Anual (R$ ou US$)": round(retorno_anual, 2),
                "Chance de Sucesso (%)": round(chance_sucesso, 2),
                "P/E Ratio": "N/A",
                "Crescimento Receita (%)": "N/A",
                "Crescimento Lucro (%)": "N/A",
                "Beta": "N/A",
                "Recomendação": "N/A"
            }

        crescimento_receita = info.get("revenueGrowth", 0) * 100
        crescimento_lucro = info.get("earningsGrowth", 0) * 100
        trailing_eps = info.get("trailingEps", None)
        recomendacao_compra = info.get("recommendationKey", "none")
        pe_ratio = preco_atual / trailing_eps if preco_atual and trailing_eps and trailing_eps != 0 else None
        recomendacao_traduzida = RECOMMENDATION_DICT.get(recomendacao_compra, "N/A")
        beta = info.get("beta", 1)

        if dividend_yield > 1 and preco_atual and pe_ratio and 5 <= pe_ratio <= 60:
            retorno_anual = (dividend_yield / 100) * preco_atual
            chance_sucesso = 0
            chance_sucesso += min(dividend_yield, 30) * 0.3
            chance_sucesso += max(0, min(crescimento_receita, 20)) * 0.25
            chance_sucesso += max(0, min(crescimento_lucro, 20)) * 0.25
            chance_sucesso -= beta * 10
            if recomendacao_traduzida == "Forte compra":
                chance_sucesso += 20
            elif recomendacao_traduzida == "Compra":
                chance_sucesso += 10
            chance_sucesso = min(max(chance_sucesso, 0), 100)

            percentual = (progresso + 1) / total * 100
            print(f"[{progresso + 1}/{total} - {percentual:.2f}%] Analisando Ação {ticker}")

            return {
                "Ticker": ticker,
                "Mercado": mercado,
                "Tipo de Ativo": tipo,
                "Setor": setor,
                "Preço Atual (R$ ou US$)": round(preco_atual, 2),
                "Dividend Yield (%)": round(dividend_yield, 2),
                "Crescimento Receita (%)": round(crescimento_receita, 2),
                "Crescimento Lucro (%)": round(crescimento_lucro, 2),
                "Beta": round(beta, 2),
                "Retorno Anual (R$ ou US$)": round(retorno_anual, 2),
                "P/E Ratio": round(pe_ratio, 2) if pe_ratio else "N/A",
                "Recomendação": recomendacao_traduzida,
                "Chance de Sucesso (%)": round(chance_sucesso, 2)
            }

    except Exception as e:
        logger.warning(f"Erro ao processar {ticker}: {e}")
    return None

def analisar_acoes():
    tickers_acao, tickers_fii = obter_lista_acoes_e_fiis()
    total = len(tickers_acao) + len(tickers_fii)
    resultados = []
    progresso = 0

    for ticker in tickers_acao:
        time.sleep(1.5)
        resultado = processar_acao(ticker, "BR", progresso, total, "acao")
        if resultado:
            resultados.append(resultado)
        progresso += 1

    for ticker in tickers_fii:
        time.sleep(1.5)
        resultado = processar_acao(ticker, "BR", progresso, total, "fii")
        if resultado:
            resultados.append(resultado)
        progresso += 1

    if resultados:
        return pd.DataFrame(sorted(resultados, key=lambda x: -x["Chance de Sucesso (%)"]))
    else:
        logger.warning("Nenhum ativo retornou dados válidos.")
        return pd.DataFrame()


def salvar_em_excel(df, filename="relatorio_acoes.xlsx"):
    df.to_excel(filename, index=False, engine="openpyxl")
    workbook = load_workbook(filename)
    sheet = workbook.active
    for col in sheet.columns:
        max_length = max(len(str(cell.value)) if cell.value else 0 for cell in col)
        sheet.column_dimensions[col[0].column_letter].width = max_length + 2
    workbook.save(filename)

def enviar_email_ses(relatorio_path):
    try:
        ses_client = boto3.client("ses", region_name=AWS_REGION)
        msg = MIMEMultipart()
        msg["Subject"] = "📊 Relatório de Ações e FIIs"
        msg["From"] = EMAIL_REMETENTE
        msg["To"] = ", ".join(EMAIL_DESTINATARIOS)
        msg.attach(MIMEText("Segue em anexo o relatório gerado.", "plain"))

        with open(relatorio_path, "rb") as file:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={relatorio_path}")
            msg.attach(part)

        ses_client.send_raw_email(
            Source=EMAIL_REMETENTE,
            Destinations=EMAIL_DESTINATARIOS,
            RawMessage={"Data": msg.as_string()}
        )
        logger.info("Email enviado com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao enviar email: {e}")

def enviar_relatorio():
    df = analisar_acoes()
    if not df.empty:
        filename = "relatorio_acoes.xlsx"
        salvar_em_excel(df, filename)
        enviar_email_ses(filename)

schedule.every().tuesday.at("05:00").do(enviar_relatorio)
schedule.every().friday.at("05:00").do(enviar_relatorio)

def executar_agendamentos():
    logger.info("Iniciando agendador...")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    enviar_relatorio()

    threading.Thread(target=executar_agendamentos, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Encerrando execução.")