from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import investpy
import yfinance as yf
import pandas as pd
import boto3
import os
from datetime import datetime
import schedule
import time

AWS_REGION = os.getenv("AWS_REGION", "sa-east-1")
EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE", "mailer@pedrovs.dev")
EMAIL_DESTINATARIO = os.getenv("EMAIL_DESTINATARIO", "pedrovs3@hotmail.com")


def obter_lista_acoes_b3():
    try:
        acoes = investpy.get_stocks(country="brazil")
        return acoes["symbol"].tolist()
    except Exception as e:
        print(f"Erro ao buscar lista de ações: {e}")
        return []


def analisar_acoes():
    tickers = obter_lista_acoes_b3()
    print(f"Analisando {len(tickers)} ações da B3...")
    melhores_acoes = []

    for ticker in tickers:
        try:
            acao = yf.Ticker(f"{ticker}.SA")
            info = acao.info

            dividend_yield = info.get("dividendYield", 0) * 100
            beta = info.get("beta", 1)
            crescimento = info.get("revenueGrowth", 0)
            preco_atual = info.get("currentPrice", None)

            if dividend_yield > 5 and beta < 1.5 and crescimento > 0 and preco_atual:
                retorno_anual = (dividend_yield / 100) * preco_atual
                melhores_acoes.append({
                    "Ticker": ticker,
                    "Preço Atual (R$)": round(preco_atual, 2),
                    "Dividend Yield (%)": round(dividend_yield, 2),
                    "Crescimento (%)": round(crescimento * 100, 2),
                    "Beta": round(beta, 2),
                    "Retorno Anual (R$)": round(retorno_anual, 2),
                })
        except Exception as e:
            print(f"Erro ao processar {ticker}: {e}")

    melhores_acoes = sorted(melhores_acoes, key=lambda x: (-x["Retorno Anual (R$)"], x["Beta"]))
    return pd.DataFrame(melhores_acoes)


def salvar_em_excel(df, filename="relatorio_acoes.xlsx"):
    if not df.empty:
        df.to_excel(filename, index=False, engine="openpyxl")
        print(f"Arquivo Excel salvo: {filename}")
    else:
        print("Nenhum dado para salvar no Excel.")


def enviar_email_ses(relatorio_path):
    try:
        ses_client = boto3.client("ses", region_name=AWS_REGION)

        subject = "📊 Relatório de Ações Promissoras"
        body_text = f"""Olá,

Segue em anexo o relatório de ações promissoras gerado em {datetime.now().strftime('%d/%m/%Y')}.
Atenciosamente,
Seu Bot de Finanças"""

        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = EMAIL_REMETENTE
        msg["To"] = EMAIL_DESTINATARIO

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
            Destinations=[EMAIL_DESTINATARIO],
            RawMessage={"Data": msg.as_string()},
        )
        print("Email enviado com sucesso via SES!")
    except Exception as e:
        print(f"Erro ao enviar email: {e}")


def enviar_relatorio():
    df = analisar_acoes()
    if not df.empty:
        excel_filename = "relatorio_acoes.xlsx"
        salvar_em_excel(df, excel_filename)
        enviar_email_ses(excel_filename)
    else:
        print("Nenhuma ação atendeu aos critérios.")


schedule.every().day.at("23:16").do(enviar_relatorio)

print("Bot de análise de ações dinâmicas iniciado. Aguardando horários agendados...")
while True:
    schedule.run_pending()
    time.sleep(1)
