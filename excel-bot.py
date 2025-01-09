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


def obter_lista_acoes_b3():
    try:
        logger.info("Buscando lista de ações da B3...")
        acoes = investpy.get_stocks(country="brazil")
        acoes_us = investpy.get_stocks(country="united states")
        logger.info(f"Lista de ações obtida com sucesso. Total: {len(acoes + acoes_us)}")
        
        simbolos_br = (acoes["symbol"] + ".SA").tolist()
        simbolos_us = acoes_us["symbol"].tolist()

        return simbolos_br + simbolos_us
    except Exception as e:
        logger.error(f"Erro ao buscar lista de ações: {e}")
        return []


def analisar_acoes_com_chance():
    logger.info("Iniciando análise de ações com chance de sucesso...")
    tickers = obter_lista_acoes_b3()
    logger.info(f"Analisando {len(tickers)} ações da B3...")

    def processar_acao(ticker):
        try:
            percentage_done = (tickers.index(ticker) + 1) / len(tickers) * 100
            logger.info(f"Progresso: {tickers.index(ticker) + 1}/{len(tickers)} - {percentage_done:.2f}%", extra={"ticker": ticker})
            logger.info(f"Processando ação: {ticker}")
            acao = yf.Ticker(ticker)
            info = acao.info

            dividend_yield = info.get("dividendYield", 0) * 100
            beta = info.get("beta", 1)
            crescimento_receita = info.get("revenueGrowth", 0) * 100
            crescimento_lucro = info.get("earningsGrowth", 0) * 100
            preco_atual = info.get("currentPrice", None)
            trailing_eps = info.get("trailingEps", None)
            recomendacao_compra = info.get("recommendationKey", "none")

            pe_ratio = preco_atual / trailing_eps if preco_atual and trailing_eps else None

            pb_ratio = info.get("priceToBook", None)
            short_percent = info.get("shortPercentOfFloat", 0) * 100
            high_52_week = info.get("fiftyTwoWeekHigh", None)
            low_52_week = info.get("fiftyTwoWeekLow", None)

            recomendacao_traduzida = RECOMMENDATION_DICT.get(recomendacao_compra, "N/A")

            if dividend_yield > 2 > beta and preco_atual and pe_ratio and 5 <= pe_ratio <= 50:
                retorno_anual = (dividend_yield / 100) * preco_atual

                chance_sucesso = 0
                chance_sucesso += min(dividend_yield, 30) * 0.3  # Peso 30%
                chance_sucesso += max(0, min(crescimento_receita, 20)) * 0.25  # Peso 25%
                chance_sucesso += max(0, min(crescimento_lucro, 20)) * 0.25  # Peso 25%
                chance_sucesso -= beta * 10  # Impacto negativo do Beta (Peso -10%)

                if recomendacao_traduzida == "Forte compra":
                    chance_sucesso += 20
                elif recomendacao_traduzida == "Compra":
                    chance_sucesso += 10

                chance_sucesso = min(max(chance_sucesso, 0), 100)  # Garantir entre 0 e 100%

                return {
                    "Ticker": ticker,
                    "Preço Atual (R$)": round(preco_atual, 2),
                    "Dividend Yield (%)": round(dividend_yield, 2),
                    "Crescimento Receita (%)": round(crescimento_receita, 2),
                    "Crescimento Lucro (%)": round(crescimento_lucro, 2),
                    "Beta": round(beta, 2),
                    "Retorno Anual (R$)": round(retorno_anual, 2),
                    "P/E Ratio": round(pe_ratio, 2) if pe_ratio else "N/A",
                    "P/B Ratio": round(pb_ratio, 2) if pb_ratio else "N/A",
                    "Short Percent (%)": round(short_percent, 2),
                    "52-Week High (R$)": round(high_52_week, 2) if high_52_week else "N/A",
                    "52-Week Low (R$)": round(low_52_week, 2) if low_52_week else "N/A",
                    "Recomendação": recomendacao_traduzida,
                    "Chance de Sucesso (%)": round(chance_sucesso, 2)
                }
        except Exception as e:
            logger.warning(f"Erro ao processar {ticker}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        resultados = list(executor.map(processar_acao, tickers))

    melhores_acoes = [acao for acao in resultados if acao]
    melhores_acoes = sorted(
        melhores_acoes,
        key=lambda x: -x["Chance de Sucesso (%)"]
    )

    logger.info(f"Análise concluída. Total de ações recomendadas: {len(melhores_acoes)}")
    return pd.DataFrame(melhores_acoes)


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
        "B": 20,  # Preço Atual
        "C": 18,  # Dividend Yield
        "D": 20,  # Crescimento Receita
        "E": 20,  # Crescimento Lucro
        "F": 10,  # Beta
        "G": 20,  # Retorno Anual
        "H": 10,  # P/E Ratio
        "I": 10,  # P/B Ratio
        "J": 20,  # Short Percent
        "K": 20,  # 52-Week High
        "L": 20,  # 52-Week Low
        "M": 25,  # Recomendação
        "N": 25,  # Chance de Sucesso
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

        response = ses_client.send_raw_email(
            Source=EMAIL_REMETENTE,
            Destinations=EMAIL_DESTINATARIOS,
            RawMessage={"Data": msg.as_string()},
        )
        logger.info("Email enviado com sucesso para: " + ", ".join(EMAIL_DESTINATARIOS))
    except Exception as e:
        logger.error(f"Erro ao enviar email: {e}")


def enviar_relatorio():
    df = analisar_acoes_com_chance()
    if not df.empty:
        excel_filename = "relatorio_acoes_chance.xlsx"
        salvar_em_excel(df, excel_filename)
        enviar_email_ses(excel_filename)
    else:
        print("Nenhuma ação atendeu aos critérios.")


schedule.every().day.at("12:00").do(enviar_relatorio)  # Agendar às 09:00 UTF
schedule.every().day.at("15:00").do(enviar_relatorio)  # Agendar às 12:00 UTF
schedule.every().day.at("20:00").do(enviar_relatorio)  # Agendar às 17:00 UTF


def executar_agendamentos():
    logger.info("Iniciando o agendador de tarefas...")
    while True:
        schedule.run_pending()
        time.sleep(1)


thread_agendamento = threading.Thread(target=executar_agendamentos, daemon=True)
thread_agendamento.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    logger.info("Encerrando o agendador.")
