from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor

import threading
import investpy
import yfinance as yf
import pandas as pd
import boto3
import os
from datetime import datetime
import schedule
import time
import logging
from openpyxl import load_workbook
from dotenv import load_dotenv

from colorama import Fore, Style
import time
from requests.exceptions import HTTPError

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


def obter_lista_acoes():
    try:
        logger.info("Buscando lista de ações...")
        acoes_br = investpy.get_stocks(country="brazil")
        acoes_us = investpy.get_stocks(country="united states")
        logger.info(f"Lista de ações obtida com sucesso. Total: {len(acoes_br) + len(acoes_us)}")

        simbolos_br = (acoes_br["symbol"] + ".SA").tolist()
        simbolos_us = acoes_us["symbol"].tolist()

        return simbolos_br, simbolos_us
    except Exception as e:
        logger.error(f"Erro ao buscar lista de ações: {e}")
        return [], []


def processar_acao(ticker, mercado, progresso, total):
    try:
        acao = yf.Ticker(ticker)

        if not acao.info or "symbol" not in acao.info:
            logger.warning(f"Ticker inválido ou não encontrado: {ticker}")
            return None

        info = acao.info

        dividend_yield = info.get("dividendYield", 0) * 100
        beta = info.get("beta", 1)
        crescimento_receita = info.get("revenueGrowth", 0) * 100
        crescimento_lucro = info.get("earningsGrowth", 0) * 100
        preco_atual = info.get("currentPrice", None)
        trailing_eps = info.get("trailingEps", None)
        recomendacao_compra = info.get("recommendationKey", "none")

        pe_ratio = preco_atual / trailing_eps if preco_atual and trailing_eps else None

        if mercado == "US":
            if not preco_atual:
                preco_atual = info.get("regularMarketPrice", None)

        recomendacao_traduzida = RECOMMENDATION_DICT.get(recomendacao_compra, "N/A")

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
            print(
                f"{Fore.GREEN}[Progresso: {progresso + 1}/{total} - {percentual:.2f}%]{Style.RESET_ALL} "
                f"Analisando {ticker} ({mercado})"
            )

            return {
                "Ticker": ticker,
                "Mercado": mercado,
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

    except HTTPError as http_err:
        if http_err.response.status_code == 429:
            logger.error(f"Erro 429 (Too Many Requests) para {ticker}. Retentando após atraso.")
            time.sleep(5)  # Atraso antes de tentar novamente
            return processar_acao(ticker, mercado, progresso, total)
        else:
            logger.error(f"Erro HTTP ao processar {ticker}: {http_err}")
    except Exception as e:
        logger.warning(f"Erro ao processar {ticker}: {e}")
    return None


def analisar_acoes():
    logger.info("Iniciando análise de ações...")
    tickers_br, tickers_us = obter_lista_acoes()
    total = len(tickers_br) + len(tickers_us)

    def limitar_taxa(ticker, mercado, progresso):
        time.sleep(0.2)  # Atraso de 200ms para cada chamada
        return processar_acao(ticker, mercado, progresso, total)

    resultados = []
    progresso = 0

    for ticker in tickers_br:
        resultado = limitar_taxa(ticker, "BR", progresso)
        if resultado:
            resultados.append(resultado)
        progresso += 1

    for ticker in tickers_us:
        resultado = limitar_taxa(ticker, "US", progresso)
        if resultado:
            resultados.append(resultado)
        progresso += 1

    todas_acoes = sorted(resultados, key=lambda x: -x["Chance de Sucesso (%)"])

    logger.info(f"Análise concluída. Total de ações recomendadas: {len(todas_acoes)}")
    return pd.DataFrame(todas_acoes)


def salvar_em_excel(df, filename="relatorio_acoes.xlsx"):
    if not df.empty:
        df.to_excel(filename, index=False, engine="openpyxl")
        ajustar_largura_colunas(filename)
    else:
        logger.error("Nenhum dado para salvar no Excel.")


def ajustar_largura_colunas(filename):
    workbook = load_workbook(filename)
    sheet = workbook.active

    colunas = {
        "A": 15,  # Ticker
        "B": 10,  # Mercado
        "C": 20,  # Preço Atual
        "D": 18,  # Dividend Yield
        "E": 20,  # Crescimento Receita
        "F": 20,  # Crescimento Lucro
        "G": 10,  # Beta
        "H": 20,  # Retorno Anual
        "I": 10,  # P/E Ratio
        "J": 25,  # Recomendação
        "K": 25,  # Chance de Sucesso
    }

    for coluna, largura in colunas.items():
        sheet.column_dimensions[coluna].width = largura

    workbook.save(filename)


def enviar_email_ses(relatorio_path):
    try:
        logger.info(f"Iniciando envio do email com o relatório: {relatorio_path}")
        ses_client = boto3.client("ses", region_name=AWS_REGION)

        subject = "📊 Relatório de Ações Promissoras"
        body_text = f"""Olá,

Segue em anexo o relatório de ações promissoras gerado em {datetime.now().strftime('%d/%m/%Y')}. 

Atenciosamente,
Seu Bot de Finanças"""

        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = EMAIL_REMETENTE
        msg["To"] = ", ".join(EMAIL_DESTINATARIOS)

        msg.attach(MIMEText(body_text, "plain"))

        with open(relatorio_path, "rb") as file:
            part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={relatorio_path.split('/')[-1]}"
            )
            msg.attach(part)

        ses_client.send_raw_email(
            Source=EMAIL_REMETENTE,
            Destinations=EMAIL_DESTINATARIOS,
            RawMessage={"Data": msg.as_string()},
        )
        logger.info("Email enviado com sucesso para: " + ", ".join(EMAIL_DESTINATARIOS))
    except Exception as e:
        logger.error(f"Erro ao enviar email: {e}")


def enviar_relatorio():
    df = analisar_acoes()
    if not df.empty:
        filename = "relatorio_acoes.xlsx"
        salvar_em_excel(df, filename)
        enviar_email_ses(filename)
    else:
        logger.info("Nenhuma ação atendeu aos critérios.")


schedule.every().day.at("12:00").do(enviar_relatorio)
schedule.every().day.at("20:00").do(enviar_relatorio)


def executar_agendamentos():
    logger.info("Agendador iniciado. Aguardando tarefas agendadas...")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    logger.info("Script iniciado. Configurando agendamentos...")

    threading.Thread(target=executar_agendamentos, daemon=True).start()

    try:
        logger.info("Executando o loop principal do programa. Pressione Ctrl+C para sair.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Encerrando o script.")
