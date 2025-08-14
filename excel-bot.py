from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from curl_cffi import requests

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
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from dotenv import load_dotenv
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
    """Carrega dados do cache se o arquivo existir e não estiver expirado."""
    path = os.path.join(CACHE_DIR, f"{ticker}.json")
    if not os.path.exists(path):
        return None
    mod_time = datetime.fromtimestamp(os.path.getmtime(path))
    if datetime.now() - mod_time > CACHE_TTL:
        return None
    with open(path, "r") as f:
        return json.load(f)


def salvar_cache(ticker, info):
    """Salva os dados de um ticker no cache."""
    path = os.path.join(CACHE_DIR, f"{ticker}.json")
    with open(path, "w") as f:
        json.dump(info, f, indent=4)


def obter_lista_acoes_e_fiis():
    """Obtém a lista de ações e FIIs da B3 utilizando investpy."""
    try:
        logger.info("Buscando lista de ativos via investpy...")
        ativos_br = investpy.get_stocks(country="brazil")
        ativos_br["symbol"] = ativos_br["symbol"].str.strip()

        # FIIs geralmente terminam com '11'
        fiis = ativos_br[ativos_br["symbol"].str.endswith("11")]
        # Ações são o restante
        acoes = ativos_br[~ativos_br["symbol"].str.endswith("11")]

        # Adiciona o sufixo '.SA' para compatibilidade com yfinance
        tickers_acoes = (acoes["symbol"] + ".SA").tolist()
        tickers_fiis = (fiis["symbol"] + ".SA").tolist()

        logger.info(f"Ações encontradas: {len(tickers_acoes)} | FIIs encontrados: {len(tickers_fiis)}")
        return tickers_acoes, tickers_fiis
    except Exception as e:
        logger.error(f"Erro ao buscar lista de ativos via investpy: {e}")
        return [], []


def processar_ativo(ticker, mercado, progresso, total, tipo_ativo="acao"):
    """Processa um único ativo (ação ou FII) para coletar e avaliar seus dados."""
    try:
        # A linha "session = requests.Session(impersonate="chrome")" foi removida.
        info = carregar_cache(ticker)
        if not info:
            # Deixamos o yfinance gerenciar sua própria sessão
            acao = yf.Ticker(ticker)
            info = acao.info
            if not info or "symbol" not in info:
                logger.warning(f"Ticker inválido ou não encontrado: {ticker}")
                return None
            salvar_cache(ticker, info)
        else:
            logger.info(f"Usando cache para {ticker}")

        # O restante da função continua igual...
        preco_atual = info.get("currentPrice") or info.get("regularMarketPrice")
        dividend_yield = info.get("dividendYield", 0) * 100
        setor = info.get("sector", "N/A")

        percentual = (progresso + 1) / total * 100
        print(f"[{progresso + 1}/{total} - {percentual:.2f}%] Analisando {tipo_ativo.upper()} {ticker}")

        if tipo_ativo == "fii":
            if not preco_atual or dividend_yield <= 5:  # Filtro básico de DY
                return None

            pvp = info.get("priceToBook")
            volume_medio = info.get("averageVolume")
            liquidez_diaria = (volume_medio or 0) * (preco_atual or 0)

            # --- Sistema de Avaliação para FIIs (0 a 100) ---
            chance_sucesso = 0
            # 1. Dividend Yield (Peso: 40) - Essencial para FIIs
            chance_sucesso += min(dividend_yield, 15) * (40 / 15)  # Normaliza até 15% de DY

            # 2. P/VP (Preço/Valor Patrimonial) (Peso: 40) - Principal métrica de valuation
            if pvp:
                if pvp <= 0.95:
                    chance_sucesso += 40
                elif pvp <= 1.05:
                    chance_sucesso += 30  # Na média ou um pouco acima
                elif pvp <= 1.15:
                    chance_sucesso += 15  # Um pouco caro
                # Se > 1.15, não adiciona pontos (penalidade implícita)

            # 3. Liquidez (Peso: 20) - Importante para conseguir comprar/vender
            if liquidez_diaria > 1000000:  # Acima de R$ 1 milhão/dia
                chance_sucesso += 20
            elif liquidez_diaria > 500000:  # Acima de R$ 500 mil/dia
                chance_sucesso += 10

            chance_sucesso = min(max(chance_sucesso, 0), 100)

            return {
                "Ticker": ticker,
                "Setor": setor,
                "Preço Atual (R$)": round(preco_atual, 2) if preco_atual else "N/A",
                "Dividend Yield (%)": round(dividend_yield, 2),
                "P/VP": round(pvp, 2) if pvp else "N/A",
                "Liquidez Média (R$)": f"{liquidez_diaria:,.2f}",
                "Chance de Sucesso (%)": round(chance_sucesso, 2),
            }

        elif tipo_ativo == "acao":
            pe_ratio = info.get("trailingPE")
            roe = info.get("returnOnEquity")
            debt_to_equity = info.get("debtToEquity")
            beta = info.get("beta", 1.0)
            recomendacao = RECOMMENDATION_DICT.get(info.get("recommendationKey", "none"), "N/A")

            if not all([preco_atual, dividend_yield > 1, pe_ratio and pe_ratio > 0]):
                return None

            # --- Sistema de Avaliação para Ações (0 a 100) ---
            chance_sucesso = 0
            # 1. Valuation (P/L) (Peso: 25) - Barato é bom, mas não demais.
            if 1 < pe_ratio <= 10:
                chance_sucesso += 25
            elif 10 < pe_ratio <= 18:
                chance_sucesso += 15

            # 2. Rentabilidade (ROE) (Peso: 25) - Eficiência da empresa.
            if roe and roe > 0.20:
                chance_sucesso += 25  # ROE > 20%
            elif roe and roe > 0.15:
                chance_sucesso += 15  # ROE > 15%

            # 3. Dividendos (DY) (Peso: 20) - Retorno ao acionista.
            chance_sucesso += min(dividend_yield, 10) * 2  # Normaliza até 10% de DY

            # 4. Endividamento (Dívida/PL) (Peso: 15) - Saúde financeira.
            if debt_to_equity:
                if debt_to_equity < 50:
                    chance_sucesso += 15  # Dívida < 0.5x PL
                elif debt_to_equity < 100:
                    chance_sucesso += 7  # Dívida < 1.0x PL
            else:  # Ausência de dívida é positivo
                chance_sucesso += 15

            # 5. Recomendação de Analistas (Peso: 15)
            if recomendacao == "Forte compra":
                chance_sucesso += 15
            elif recomendacao == "Compra":
                chance_sucesso += 10

            # Penalidade por Risco (Beta) - Ações mais voláteis que o mercado perdem pontos
            if beta and beta > 1.2:
                chance_sucesso -= (beta - 1.2) * 10

            chance_sucesso = min(max(chance_sucesso, 0), 100)

            return {
                "Ticker": ticker,
                "Setor": setor,
                "Preço Atual (R$)": round(preco_atual, 2),
                "Dividend Yield (%)": round(dividend_yield, 2),
                "P/L": round(pe_ratio, 2) if pe_ratio else "N/A",
                "ROE (%)": round(roe * 100, 2) if roe else "N/A",
                "Dívida/PL": round(debt_to_equity / 100, 2) if debt_to_equity else "N/A",  # yf retorna em %
                "Beta": round(beta, 2) if beta else "N/A",
                "Recomendação": recomendacao,
                "Chance de Sucesso (%)": round(chance_sucesso, 2)
            }

    except Exception as e:
        logger.warning(f"Erro ao processar {ticker}: {e}")
    return None

def analisar_ativos():
    """Busca, processa e classifica ações e FIIs."""
    tickers_acao, tickers_fii = obter_lista_acoes_e_fiis()
    total_ativos = len(tickers_acao) + len(tickers_fii)
    resultados_acoes, resultados_fiis = [], []
    progresso = 0

    # Processa Ações
    for ticker in tickers_acao:
        time.sleep(1)  # Evita sobrecarregar a API
        resultado = processar_ativo(ticker, "BR", progresso, total_ativos, "acao")
        if resultado:
            resultados_acoes.append(resultado)
        progresso += 1

    # Processa FIIs
    for ticker in tickers_fii:
        time.sleep(1)  # Evita sobrecarregar a API
        resultado = processar_ativo(ticker, "BR", progresso, total_ativos, "fii")
        if resultado:
            resultados_fiis.append(resultado)
        progresso += 1

    df_acoes = pd.DataFrame()
    df_fiis = pd.DataFrame()

    if resultados_acoes:
        df_acoes = pd.DataFrame(sorted(resultados_acoes, key=lambda x: -x["Chance de Sucesso (%)"]))
    else:
        logger.warning("Nenhuma ação retornou dados válidos.")

    if resultados_fiis:
        df_fiis = pd.DataFrame(sorted(resultados_fiis, key=lambda x: -x["Chance de Sucesso (%)"]))
    else:
        logger.warning("Nenhum FII retornou dados válidos.")

    return df_acoes, df_fiis


def salvar_em_excel(df_acoes, df_fiis, filename="relatorio_investimentos.xlsx"):
    """Salva os DataFrames de ações e FIIs em abas separadas de um arquivo Excel."""
    with pd.ExcelWriter(filename, engine='openpyxl') as writer:
        # Salva Ações na primeira aba
        if not df_acoes.empty:
            df_acoes.to_excel(writer, sheet_name='Ações', index=False)
            logger.info(f"DataFrame de Ações salvo na aba 'Ações'.")

        # Salva FIIs na segunda aba
        if not df_fiis.empty:
            df_fiis.to_excel(writer, sheet_name='FIIs', index=False)
            logger.info(f"DataFrame de FIIs salvo na aba 'FIIs'.")

        # Ajusta a largura das colunas para cada aba
        for sheet_name in writer.sheets:
            sheet = writer.sheets[sheet_name]
            for col in sheet.columns:
                max_length = 0
                column = col[0].column_letter  # Pega a letra da coluna
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = (max_length + 2)
                sheet.column_dimensions[column].width = adjusted_width

    logger.info(f"Relatório salvo com sucesso em '{filename}'.")


def enviar_email_ses(relatorio_path):
    """Envia o relatório por e-mail usando o AWS SES."""
    try:
        ses_client = boto3.client("ses", region_name=AWS_REGION)
        msg = MIMEMultipart()
        msg["Subject"] = f"📊 Relatório de Ações e FIIs - {datetime.now().strftime('%d/%m/%Y')}"
        msg["From"] = EMAIL_REMETENTE
        msg["To"] = ", ".join(EMAIL_DESTINATARIOS)
        msg.attach(MIMEText("Segue em anexo o relatório de análise de ativos da bolsa.", "plain"))

        with open(relatorio_path, "rb") as file:
            part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(relatorio_path)}")
            msg.attach(part)

        ses_client.send_raw_email(
            Source=EMAIL_REMETENTE,
            Destinations=EMAIL_DESTINATARIOS,
            RawMessage={"Data": msg.as_string()}
        )
        logger.info("Email enviado com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao enviar email via SES: {e}")


def enviar_relatorio():
    """Função principal que orquestra a análise e o envio."""
    logger.info("Iniciando a geração do relatório...")
    df_acoes, df_fiis = analisar_ativos()

    if not df_acoes.empty or not df_fiis.empty:
        filename = "relatorio_investimentos.xlsx"
        salvar_em_excel(df_acoes, df_fiis, filename)
        enviar_email_ses(filename)
    else:
        logger.warning("Nenhum dado para gerar relatório. O e-mail não será enviado.")


# --- Agendamento ---
# Descomente as linhas abaixo para agendar a execução
schedule.every().tuesday.at("05:00").do(enviar_relatorio)
schedule.every().friday.at("05:00").do(enviar_relatorio)

def executar_agendamentos():
    logger.info("Iniciando agendador de tarefas...")
    while True:
        schedule.run_pending()
        time.sleep(60)  # Verifica a cada minuto


if __name__ == "__main__":
    # Executa uma vez ao iniciar o script
    enviar_relatorio()

    # Inicia o agendador em uma thread separada para não bloquear
    # Descomente a linha abaixo para ativar o agendamento contínuo
    threading.Thread(target=executar_agendamentos, daemon=True).start()

    logger.info("Execução principal concluída.")
    # Mantém o script rodando se o agendador estiver ativo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Encerrando execução.")
