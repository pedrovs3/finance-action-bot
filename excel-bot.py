from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import pandas as pd
import smtplib
import os
import json
from datetime import datetime, timedelta
import schedule
import time
import logging
from dotenv import load_dotenv

# ==== Configuração Inicial ====
load_dotenv()

GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")
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

# ==== Estratégia de Investimento ====
# Valores válidos: "top-dividendos", "patrimonios-estaveis", "valorizacao", "dividendos-consistentes"
ESTRATEGIA_INVESTIMENTO = os.getenv("ESTRATEGIA_INVESTIMENTO", "top-dividendos")

ESTRATEGIAS_VALIDAS = ("top-dividendos", "patrimonios-estaveis", "valorizacao", "dividendos-consistentes")
if ESTRATEGIA_INVESTIMENTO not in ESTRATEGIAS_VALIDAS:
    raise ValueError(
        f"ESTRATEGIA_INVESTIMENTO inválida: '{ESTRATEGIA_INVESTIMENTO}'. "
        f"Valores válidos: {', '.join(ESTRATEGIAS_VALIDAS)}"
    )

# =============================================================================
# PESOS PARA AÇÕES — cada critério recebe um peso; a soma é 100.
# =============================================================================
#
# Critérios disponíveis para ações:
#   dividendos      — Dividend Yield atual
#   valuation       — P/L (Trailing P/E) e P/VP (Price/Book)
#   rentabilidade   — ROE e margens operacionais
#   endividamento   — Dívida/PL e liquidez corrente
#   crescimento     — Crescimento de receita e lucro
#   recomendacao    — Consenso de analistas e upside vs preço-alvo
#   risco           — Beta e variação 52 semanas
#
# top-dividendos:
#   Prioriza retorno passivo via dividendos e consistência de pagamento.
#   DY alto é rei; endividamento baixo garante sustentabilidade dos proventos.
#
# patrimonios-estaveis:
#   Busca empresas sólidas com crescimento moderado e previsível.
#   Equilíbrio entre todas as métricas: ROE forte, dívida baixa,
#   dividendos razoáveis e valuations justos. Risco baixo é valorizado.
#
# valorizacao:
#   Foco em upside de preço e crescimento agressivo.
#   ROE alto indica eficiência; P/L baixo indica desconto;
#   crescimento de receita e recomendação de analistas validam a tese.
#   Dividendos são irrelevantes (lucro deve ser reinvestido).
#
# dividendos-consistentes:
#   Busca empresas estáveis que pagam bons dividendos com frequência.
#   Diferente de top-dividendos, prioriza sustentabilidade e previsibilidade.
#   Endividamento baixo, ROE forte e beta mínimo garantem que os dividendos
#   não serão cortados. Aceita DY moderado (3%+) se a empresa for sólida.

PESOS_POR_ESTRATEGIA = {
    "top-dividendos": {
        "dividendos":    30,  # DY é o critério dominante
        "valuation":     15,  # P/L e P/VP — não pagar caro pelo yield
        "rentabilidade": 10,  # ROE e margens — secundário
        "endividamento": 20,  # Dívida baixa sustenta dividendos
        "crescimento":   5,   # Pouco relevante para renda passiva
        "recomendacao":  5,   # Consenso de mercado
        "risco":         15,  # Beta baixo = estabilidade dos proventos
    },
    "patrimonios-estaveis": {
        "dividendos":    10,  # Algum retorno, mas não prioridade
        "valuation":     20,  # Não pagar caro
        "rentabilidade": 20,  # Empresa eficiente e lucrativa
        "endividamento": 20,  # Saúde financeira sólida
        "crescimento":   10,  # Crescimento moderado desejável
        "recomendacao":  5,   # Opinião de analistas
        "risco":         15,  # Beta baixo é muito valorizado
    },
    "valorizacao": {
        "dividendos":    0,   # Irrelevante — lucro deve ser reinvestido
        "valuation":     20,  # P/L baixo = potencial de re-rating
        "rentabilidade": 25,  # ROE alto = máquina de crescimento
        "endividamento": 5,   # Tolerância maior à alavancagem
        "crescimento":   20,  # Crescimento de receita/lucro é essencial
        "recomendacao":  15,  # Analistas validam tese de crescimento
        "risco":         15,  # Aceita mais risco, mas penaliza excessos
    },
    "dividendos-consistentes": {
        "dividendos":    25,  # Bom DY, mas não precisa ser o maior
        "valuation":     10,  # Preço justo, sem pagar caro
        "rentabilidade": 15,  # ROE forte sustenta proventos no longo prazo
        "endividamento": 20,  # Dívida baixa é crucial para manter dividendos
        "crescimento":   5,   # Pouco relevante — foco em estabilidade
        "recomendacao":  5,   # Pouco relevante
        "risco":         20,  # Beta baixo = estabilidade de preço e previsibilidade
    },
}

# Filtros mínimos por estratégia para AÇÕES
# dy_min: Dividend Yield mínimo para considerar o ativo (%)
# pe_min: P/L mínimo (descarta negativos / muito distorcidos)
FILTROS_POR_ESTRATEGIA = {
    "top-dividendos":          {"dy_min": 4.0, "pe_min": 1.0},
    "patrimonios-estaveis":    {"dy_min": 1.0, "pe_min": 1.0},
    "valorizacao":             {"dy_min": 0.0, "pe_min": 1.0},
    "dividendos-consistentes": {"dy_min": 3.0, "pe_min": 1.0},
}

# =============================================================================
# PESOS PARA FIIs — critérios adaptados à natureza dos fundos imobiliários.
# =============================================================================
#
# Critérios disponíveis para FIIs:
#   dividendos     — Dividend Yield mensal/anualizado
#   valuation      — P/VP (Price/Book) quando disponível, senão spread vs média 52s
#   estabilidade   — Variação de preço em 52 semanas (quanto menor, mais estável)
#   liquidez       — Volume financeiro médio diário (capacidade de entrar/sair)
#   momentum       — Preço atual vs média 200 dias (tendência de médio prazo)

PESOS_FII_POR_ESTRATEGIA = {
    "top-dividendos": {
        "dividendos":    40,  # DY é o fator principal
        "valuation":     25,  # P/VP baixo = desconto patrimonial
        "estabilidade":  10,  # Alguma estabilidade, mas DY compensa
        "liquidez":      15,  # Precisa conseguir comprar/vender
        "momentum":      10,  # Tendência positiva é bônus
    },
    "patrimonios-estaveis": {
        "dividendos":    20,  # Retorno razoável
        "valuation":     25,  # Não comprar caro
        "estabilidade":  25,  # Pouca oscilação de preço
        "liquidez":      15,  # Liquidez adequada
        "momentum":      15,  # Tendência estável/positiva
    },
    "valorizacao": {
        "dividendos":    10,  # Pouco relevante
        "valuation":     30,  # P/VP com desconto = potencial de valorização
        "estabilidade":  10,  # Aceita mais oscilação
        "liquidez":      20,  # Liquidez alta para arbitragem
        "momentum":      30,  # Momentum forte = upside
    },
    "dividendos-consistentes": {
        "dividendos":    30,  # Bom DY, mas sustentável
        "valuation":     15,  # Preço justo
        "estabilidade":  30,  # Estabilidade de preço é prioridade máxima
        "liquidez":      10,  # Mínimo necessário
        "momentum":      15,  # Tendência estável
    },
}

# Filtros mínimos por estratégia para FIIs
# dy_min: Dividend Yield mínimo (%)
# liquidez_min: Volume financeiro diário mínimo (R$)
FILTROS_FII_POR_ESTRATEGIA = {
    "top-dividendos":          {"dy_min": 6.0, "liquidez_min": 100_000},
    "patrimonios-estaveis":    {"dy_min": 4.0, "liquidez_min": 200_000},
    "valorizacao":             {"dy_min": 2.0, "liquidez_min": 300_000},
    "dividendos-consistentes": {"dy_min": 5.0, "liquidez_min": 100_000},
}

NOMES_ESTRATEGIAS = {
    "top-dividendos": "Top Dividendos",
    "patrimonios-estaveis": "Patrimônios Estáveis com Valorização",
    "valorizacao": "Foco em Valorização",
    "dividendos-consistentes": "Dividendos Estáveis e Consistentes",
}

logger.info(f"Estratégia de investimento: {NOMES_ESTRATEGIAS[ESTRATEGIA_INVESTIMENTO]}")

# ==== Configuração de Concorrência ====
MAX_WORKERS = 5          # Threads simultâneas para processar ativos
MAX_REQUESTS_PER_SEC = 4 # Limite de requisições por segundo à API
MAX_RETRIES = 3          # Tentativas em caso de falha/timeout
RETRY_BASE_DELAY = 2     # Delay base (segundos) para backoff exponencial


class LimitadorDeRequisicoes:
    """Limitador de taxa thread-safe usando token bucket simplificado."""

    def __init__(self, requisicoes_por_segundo):
        self._intervalo_minimo = 1.0 / requisicoes_por_segundo
        self._ultimo_request = 0.0
        self._lock = threading.Lock()

    def aguardar(self):
        """Bloqueia a thread até que seja permitido fazer a próxima requisição."""
        with self._lock:
            agora = time.monotonic()
            tempo_espera = self._ultimo_request + self._intervalo_minimo - agora
            if tempo_espera > 0:
                time.sleep(tempo_espera)
            self._ultimo_request = time.monotonic()


limitador_api = LimitadorDeRequisicoes(MAX_REQUESTS_PER_SEC)


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
    """Obtém a lista de ações e FIIs da B3 a partir do CSV de dados de ações."""
    try:
        logger.info("Buscando lista de ativos a partir dos dados de ações brasileiras...")

        # Lê o CSV embutido no pacote investpy (evita importar o módulo que depende de pkg_resources)
        import importlib.util
        spec = importlib.util.find_spec("investpy")
        if spec and spec.origin:
            csv_path = os.path.join(os.path.dirname(spec.origin), "resources", "stocks.csv")
        else:
            raise FileNotFoundError("Pacote investpy não encontrado. Instale com: pip install investpy")

        ativos_br = pd.read_csv(csv_path)
        ativos_br = ativos_br[ativos_br["country"] == "brazil"].copy()
        ativos_br.loc[:, "symbol"] = ativos_br["symbol"].str.strip()

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
        logger.error(f"Erro ao buscar lista de ativos: {e}")
        return [], []


def processar_ativo(ticker, mercado, progresso, total, tipo_ativo="acao"):
    """Processa um único ativo (ação ou FII) para coletar e avaliar seus dados."""
    try:
        info = carregar_cache(ticker)
        if not info:
            # Aguarda o limitador de taxa antes de fazer requisição à API
            limitador_api.aguardar()

            # Retry com backoff exponencial para lidar com timeouts
            for tentativa in range(MAX_RETRIES):
                try:
                    acao = yf.Ticker(ticker)
                    info = acao.info
                    break
                except Exception as e:
                    if tentativa < MAX_RETRIES - 1:
                        delay = RETRY_BASE_DELAY * (2 ** tentativa)
                        logger.warning(f"Tentativa {tentativa + 1}/{MAX_RETRIES} falhou para {ticker}: {e}. Aguardando {delay}s...")
                        time.sleep(delay)
                    else:
                        logger.warning(f"Todas as {MAX_RETRIES} tentativas falharam para {ticker}: {e}")
                        return None

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
            pvp = info.get("priceToBook")
            volume_medio = info.get("averageVolume", 0)
            liquidez_diaria = (volume_medio or 0) * (preco_atual or 0)
            variacao_52s = info.get("52WeekChange")  # decimal (0.10 = +10%)
            media_200d = info.get("twoHundredDayAverage")
            high_52 = info.get("fiftyTwoWeekHigh")
            low_52 = info.get("fiftyTwoWeekLow")

            pesos_fii = PESOS_FII_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
            filtros_fii = FILTROS_FII_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]

            # Filtros mínimos
            if not preco_atual:
                return None
            if dividend_yield < filtros_fii["dy_min"]:
                return None
            if liquidez_diaria < filtros_fii["liquidez_min"]:
                return None

            # --- Sistema de Avaliação para FIIs (0 a 100) ---
            chance_sucesso = 0

            # 1. Dividendos (DY) — normaliza linearmente até 14%
            #    DY de FII acima de 14% geralmente indica risco ou cota depreciada
            peso = pesos_fii["dividendos"]
            if peso > 0:
                chance_sucesso += min(dividend_yield / 14.0, 1.0) * peso

            # 2. Valuation (P/VP) — preço vs valor patrimonial
            #    P/VP < 1 = desconto; P/VP ~1 = justo; P/VP > 1.15 = caro
            #    Quando P/VP não disponível, usa spread 52 semanas como proxy
            peso = pesos_fii["valuation"]
            if peso > 0:
                if pvp is not None and pvp > 0:
                    if pvp <= 0.85:
                        chance_sucesso += peso               # Grande desconto
                    elif pvp <= 0.95:
                        chance_sucesso += peso * 0.80        # Desconto moderado
                    elif pvp <= 1.05:
                        chance_sucesso += peso * 0.55        # Valor justo
                    elif pvp <= 1.15:
                        chance_sucesso += peso * 0.20        # Ligeiramente caro
                    # Acima de 1.15: 0 pontos
                elif high_52 and low_52 and high_52 > low_52:
                    # Sem P/VP: usa posição relativa na faixa 52s
                    # Quanto mais perto do low, melhor o "valuation"
                    posicao = (preco_atual - low_52) / (high_52 - low_52)
                    chance_sucesso += max(1.0 - posicao, 0) * peso * 0.70

            # 3. Estabilidade — oscilação de preço em 52 semanas
            #    Faixa estreita entre high e low = FII mais estável
            peso = pesos_fii["estabilidade"]
            if peso > 0 and high_52 and low_52 and low_52 > 0:
                oscilacao = (high_52 - low_52) / low_52  # Amplitude percentual
                if oscilacao <= 0.10:
                    chance_sucesso += peso               # < 10% oscilação = excelente
                elif oscilacao <= 0.20:
                    chance_sucesso += peso * 0.75        # 10-20% = boa
                elif oscilacao <= 0.30:
                    chance_sucesso += peso * 0.40        # 20-30% = razoável
                elif oscilacao <= 0.45:
                    chance_sucesso += peso * 0.15        # 30-45% = volátil
                # Acima de 45%: 0 pontos (muito instável)

            # 4. Liquidez — volume financeiro médio diário
            #    FII sem liquidez pode ser uma armadilha: difícil sair
            peso = pesos_fii["liquidez"]
            if peso > 0:
                if liquidez_diaria >= 2_000_000:
                    chance_sucesso += peso               # Excelente liquidez
                elif liquidez_diaria >= 1_000_000:
                    chance_sucesso += peso * 0.75        # Boa liquidez
                elif liquidez_diaria >= 500_000:
                    chance_sucesso += peso * 0.45        # Liquidez OK
                elif liquidez_diaria >= 200_000:
                    chance_sucesso += peso * 0.20        # Liquidez baixa
                # Abaixo: já filtrado por liquidez_min

            # 5. Momentum — preço atual vs média 200 dias
            #    Acima da média = tendência positiva; abaixo = tendência negativa
            peso = pesos_fii["momentum"]
            if peso > 0 and media_200d and media_200d > 0:
                ratio = preco_atual / media_200d
                if ratio >= 1.05:
                    chance_sucesso += peso               # 5%+ acima da média
                elif ratio >= 1.00:
                    chance_sucesso += peso * 0.70        # Na média ou pouco acima
                elif ratio >= 0.95:
                    chance_sucesso += peso * 0.35        # Pouco abaixo
                elif ratio >= 0.90:
                    chance_sucesso += peso * 0.10        # Abaixo
                # Mais de 10% abaixo: 0 pontos

            chance_sucesso = min(max(round(chance_sucesso, 2), 0), 100)

            # Calcula spread vs 52s para exibição
            spread_52s = ""
            if high_52 and low_52 and low_52 > 0:
                oscilacao_pct = (high_52 - low_52) / low_52 * 100
                spread_52s = f"{oscilacao_pct:.1f}%"

            return {
                "Ticker": ticker,
                "Setor": setor,
                "Preço Atual (R$)": round(preco_atual, 2) if preco_atual else "N/A",
                "Dividend Yield (%)": round(dividend_yield, 2),
                "P/VP": round(pvp, 2) if pvp else "N/A",
                "Oscilação 52s": spread_52s if spread_52s else "N/A",
                "Liquidez Média (R$)": f"{liquidez_diaria:,.2f}",
                "Vs Média 200d (%)": round((preco_atual / media_200d - 1) * 100, 2) if media_200d and media_200d > 0 else "N/A",
                "Estratégia": NOMES_ESTRATEGIAS[ESTRATEGIA_INVESTIMENTO],
                "Chance de Sucesso (%)": round(chance_sucesso, 2),
            }

        elif tipo_ativo == "acao":
            pe_ratio = info.get("trailingPE")
            forward_pe = info.get("forwardPE")
            roe = info.get("returnOnEquity")
            debt_to_equity = info.get("debtToEquity")
            current_ratio = info.get("currentRatio")
            beta = info.get("beta", 1.0)
            preco_book = info.get("priceToBook")
            margem_operacional = info.get("operatingMargins")
            cresc_receita = info.get("revenueGrowth")      # decimal (-0.05 = -5%)
            cresc_lucro = info.get("earningsGrowth")        # decimal (0.10 = +10%)
            preco_alvo = info.get("targetMeanPrice")
            variacao_52s = info.get("52WeekChange")         # decimal
            recomendacao_key = info.get("recommendationKey", "none")
            recomendacao = RECOMMENDATION_DICT.get(recomendacao_key, "N/A")

            pesos = PESOS_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
            filtros = FILTROS_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]

            # Filtros mínimos dependem da estratégia
            if not preco_atual:
                return None
            if dividend_yield < filtros["dy_min"]:
                return None
            if not pe_ratio or pe_ratio < filtros["pe_min"]:
                return None

            # --- Sistema de Avaliação para Ações (0 a 100) ---
            chance_sucesso = 0

            # 1. Dividendos (DY) — normaliza linearmente até 12%
            #    Penaliza DY > 15% (pode indicar empresa em dificuldades pagando
            #    dividendo insustentável ou queda abrupta no preço da ação)
            peso = pesos["dividendos"]
            if peso > 0:
                if dividend_yield <= 15:
                    chance_sucesso += min(dividend_yield / 12.0, 1.0) * peso
                else:
                    # DY extremo (> 15%): provável armadilha, penaliza
                    chance_sucesso += peso * 0.40

            # 2. Valuation — combina P/L e P/VP para uma visão mais completa
            #    P/L sozinho engana em setores capital-intensivos; P/VP complementa
            peso = pesos["valuation"]
            if peso > 0:
                nota_pl = 0
                nota_pvp = 0

                # P/L: usa forward P/E quando disponível (mais preditivo)
                pe_usado = forward_pe if forward_pe and forward_pe > 0 else pe_ratio
                if pe_usado and pe_usado > 0:
                    if pe_usado <= 6:
                        nota_pl = 1.0                    # Muito barato
                    elif pe_usado <= 10:
                        nota_pl = 0.80                   # Barato
                    elif pe_usado <= 15:
                        nota_pl = 0.55                   # Justo
                    elif pe_usado <= 22:
                        nota_pl = 0.25                   # Caro
                    elif pe_usado <= 30:
                        nota_pl = 0.08                   # Muito caro

                # P/VP: complementa especialmente para setores asset-heavy
                if preco_book and preco_book > 0:
                    if preco_book <= 1.0:
                        nota_pvp = 1.0                   # Abaixo do patrimônio
                    elif preco_book <= 1.5:
                        nota_pvp = 0.75
                    elif preco_book <= 2.5:
                        nota_pvp = 0.45
                    elif preco_book <= 4.0:
                        nota_pvp = 0.15
                    # P/VP > 4: 0

                # Combina: 65% P/L + 35% P/VP (P/L é mais universal)
                if preco_book and preco_book > 0:
                    chance_sucesso += (nota_pl * 0.65 + nota_pvp * 0.35) * peso
                else:
                    chance_sucesso += nota_pl * peso

            # 3. Rentabilidade — combina ROE com margem operacional
            #    ROE alto + margem alta = empresa eficiente e lucrativa
            #    ROE alto + margem baixa = pode ser alavancagem financeira
            peso = pesos["rentabilidade"]
            if peso > 0:
                nota_roe = 0
                nota_margem = 0

                if roe and roe > 0:
                    roe_pct = roe * 100
                    nota_roe = min(roe_pct / 25.0, 1.0)  # Normaliza até 25%

                if margem_operacional and margem_operacional > 0:
                    margem_pct = margem_operacional * 100
                    nota_margem = min(margem_pct / 30.0, 1.0)  # Normaliza até 30%

                # Combina: 70% ROE + 30% margem operacional
                if margem_operacional and margem_operacional > 0:
                    chance_sucesso += (nota_roe * 0.70 + nota_margem * 0.30) * peso
                else:
                    chance_sucesso += nota_roe * peso

            # 4. Endividamento — combina Dívida/PL com liquidez corrente
            #    Dívida/PL mostra alavancagem de longo prazo
            #    Liquidez corrente mostra capacidade de pagar dívidas de curto prazo
            peso = pesos["endividamento"]
            if peso > 0:
                nota_divida = 0
                nota_liquidez = 0

                if debt_to_equity is None or debt_to_equity <= 0:
                    nota_divida = 1.0                    # Sem dívida
                elif debt_to_equity < 30:
                    nota_divida = 0.90                   # Dívida < 0.3x PL
                elif debt_to_equity < 60:
                    nota_divida = 0.70                   # Dívida < 0.6x PL
                elif debt_to_equity < 100:
                    nota_divida = 0.45                   # Dívida < 1.0x PL
                elif debt_to_equity < 150:
                    nota_divida = 0.20                   # Dívida < 1.5x PL
                elif debt_to_equity < 250:
                    nota_divida = 0.05                   # Dívida < 2.5x PL
                # Acima de 250%: 0 pontos

                if current_ratio and current_ratio > 0:
                    if current_ratio >= 2.0:
                        nota_liquidez = 1.0              # Excelente liquidez
                    elif current_ratio >= 1.5:
                        nota_liquidez = 0.75
                    elif current_ratio >= 1.0:
                        nota_liquidez = 0.40             # Adequada
                    elif current_ratio >= 0.7:
                        nota_liquidez = 0.10             # Apertada
                    # Abaixo de 0.7: 0

                # Combina: 70% dívida/PL + 30% liquidez corrente
                if current_ratio and current_ratio > 0:
                    chance_sucesso += (nota_divida * 0.70 + nota_liquidez * 0.30) * peso
                else:
                    chance_sucesso += nota_divida * peso

            # 5. Crescimento — receita e lucro (indicam futuro da empresa)
            peso = pesos["crescimento"]
            if peso > 0:
                nota_cresc = 0
                componentes = 0

                if cresc_receita is not None:
                    cresc_r_pct = cresc_receita * 100
                    if cresc_r_pct >= 20:
                        nota_r = 1.0                     # Crescimento forte
                    elif cresc_r_pct >= 10:
                        nota_r = 0.75
                    elif cresc_r_pct >= 3:
                        nota_r = 0.45                    # Crescimento moderado
                    elif cresc_r_pct >= 0:
                        nota_r = 0.15                    # Estagnado
                    else:
                        nota_r = 0.0                     # Encolhendo
                    nota_cresc += nota_r
                    componentes += 1

                if cresc_lucro is not None:
                    cresc_l_pct = cresc_lucro * 100
                    if cresc_l_pct >= 25:
                        nota_l = 1.0
                    elif cresc_l_pct >= 10:
                        nota_l = 0.70
                    elif cresc_l_pct >= 0:
                        nota_l = 0.30
                    else:
                        nota_l = 0.0                     # Lucro caindo
                    nota_cresc += nota_l
                    componentes += 1

                if componentes > 0:
                    chance_sucesso += (nota_cresc / componentes) * peso

            # 6. Recomendação de Analistas — inclui upside vs preço-alvo
            peso = pesos["recomendacao"]
            if peso > 0:
                nota_consenso = 0
                nota_upside = 0

                # Consenso qualitativo
                if recomendacao == "Forte compra":
                    nota_consenso = 1.0
                elif recomendacao == "Compra":
                    nota_consenso = 0.70
                elif recomendacao == "Mantenha":
                    nota_consenso = 0.30
                elif recomendacao in ("Desempenho inferior", "Venda", "Forte venda"):
                    nota_consenso = 0.0

                # Upside quantitativo: preço-alvo vs preço atual
                if preco_alvo and preco_atual and preco_atual > 0:
                    upside = (preco_alvo - preco_atual) / preco_atual
                    if upside >= 0.30:
                        nota_upside = 1.0                # 30%+ upside
                    elif upside >= 0.15:
                        nota_upside = 0.70               # 15-30% upside
                    elif upside >= 0.05:
                        nota_upside = 0.40               # 5-15% upside
                    elif upside >= 0:
                        nota_upside = 0.15               # Pouco upside
                    else:
                        nota_upside = 0.0                # Downside

                # Combina: 50% consenso + 50% upside (se disponível)
                if preco_alvo and preco_atual:
                    chance_sucesso += (nota_consenso * 0.50 + nota_upside * 0.50) * peso
                else:
                    chance_sucesso += nota_consenso * peso

            # 7. Risco — combina Beta com variação 52 semanas
            #    Beta mede sensibilidade ao mercado
            #    Variação 52s mostra se a ação caiu muito (risco realizado)
            peso = pesos["risco"]
            if peso > 0:
                nota_beta = 0
                nota_var52 = 0

                if beta and beta > 0:
                    if beta <= 0.6:
                        nota_beta = 1.0                  # Muito defensivo
                    elif beta <= 0.8:
                        nota_beta = 0.85
                    elif beta <= 1.0:
                        nota_beta = 0.65                 # Abaixo do mercado
                    elif beta <= 1.2:
                        nota_beta = 0.35                 # Próximo ao mercado
                    elif beta <= 1.5:
                        nota_beta = 0.10                 # Volátil
                    # Acima de 1.5: 0 pontos

                # Variação 52s: queda grande = risco; alta moderada = bom
                if variacao_52s is not None:
                    var_pct = variacao_52s * 100
                    if var_pct >= 10:
                        nota_var52 = 0.80                # Subiu bem, tendência OK
                    elif var_pct >= 0:
                        nota_var52 = 0.65                # Estável
                    elif var_pct >= -15:
                        nota_var52 = 0.35                # Queda moderada
                    elif var_pct >= -30:
                        nota_var52 = 0.10                # Queda forte
                    else:
                        nota_var52 = 0.0                 # Colapso (> -30%)

                # Combina: 60% beta + 40% variação 52s
                if variacao_52s is not None:
                    chance_sucesso += (nota_beta * 0.60 + nota_var52 * 0.40) * peso
                else:
                    chance_sucesso += nota_beta * peso

            chance_sucesso = min(max(round(chance_sucesso, 2), 0), 100)

            return {
                "Ticker": ticker,
                "Setor": setor,
                "Preço Atual (R$)": round(preco_atual, 2),
                "Dividend Yield (%)": round(dividend_yield, 2),
                "P/L": round(pe_ratio, 2) if pe_ratio else "N/A",
                "P/L Forward": round(forward_pe, 2) if forward_pe else "N/A",
                "P/VP": round(preco_book, 2) if preco_book else "N/A",
                "ROE (%)": round(roe * 100, 2) if roe else "N/A",
                "Margem Op. (%)": round(margem_operacional * 100, 2) if margem_operacional else "N/A",
                "Dívida/PL": round(debt_to_equity / 100, 2) if debt_to_equity else "N/A",
                "Liquidez Corrente": round(current_ratio, 2) if current_ratio else "N/A",
                "Cresc. Receita (%)": round(cresc_receita * 100, 2) if cresc_receita is not None else "N/A",
                "Beta": round(beta, 2) if beta else "N/A",
                "Recomendação": recomendacao,
                "Upside vs Alvo (%)": round((preco_alvo - preco_atual) / preco_atual * 100, 2) if preco_alvo and preco_atual else "N/A",
                "Estratégia": NOMES_ESTRATEGIAS[ESTRATEGIA_INVESTIMENTO],
                "Chance de Sucesso (%)": chance_sucesso,
            }

    except Exception as e:
        logger.warning(f"Erro ao processar {ticker}: {e}")
    return None

def _processar_lote_paralelo(tickers, tipo_ativo, total_geral, offset_progresso):
    """Processa uma lista de tickers em paralelo usando ThreadPoolExecutor."""
    resultados = []
    concluidos = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                processar_ativo, ticker, "BR", offset_progresso + i, total_geral, tipo_ativo
            ): ticker
            for i, ticker in enumerate(tickers)
        }

        for future in as_completed(futures):
            ticker = futures[future]
            concluidos += 1
            try:
                resultado = future.result()
                if resultado:
                    resultados.append(resultado)
            except Exception as e:
                logger.warning(f"Erro inesperado ao processar {ticker}: {e}")

            if concluidos % 50 == 0:
                logger.info(f"Progresso {tipo_ativo}: {concluidos}/{len(tickers)} concluídos")

    return resultados


def analisar_ativos():
    """Busca, processa e classifica ações e FIIs."""
    tickers_acao, tickers_fii = obter_lista_acoes_e_fiis()
    total_ativos = len(tickers_acao) + len(tickers_fii)

    logger.info(f"Iniciando análise de {total_ativos} ativos com {MAX_WORKERS} workers (limite: {MAX_REQUESTS_PER_SEC} req/s)...")

    # Processa Ações e FIIs em paralelo com rate limiting
    resultados_acoes = _processar_lote_paralelo(tickers_acao, "acao", total_ativos, 0)
    resultados_fiis = _processar_lote_paralelo(tickers_fii, "fii", total_ativos, len(tickers_acao))

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
    nome_estrategia = NOMES_ESTRATEGIAS[ESTRATEGIA_INVESTIMENTO]

    with pd.ExcelWriter(filename, engine='openpyxl') as writer:
        # Salva Ações na primeira aba
        if not df_acoes.empty:
            df_acoes.to_excel(writer, sheet_name='Ações', index=False)
            logger.info(f"DataFrame de Ações salvo na aba 'Ações'.")

        # Salva FIIs na segunda aba
        if not df_fiis.empty:
            df_fiis.to_excel(writer, sheet_name='FIIs', index=False)
            logger.info(f"DataFrame de FIIs salvo na aba 'FIIs'.")

        # Salva resumo da estratégia utilizada
        pesos_acoes = PESOS_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
        pesos_fiis = PESOS_FII_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
        filtros_acoes = FILTROS_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
        filtros_fiis = FILTROS_FII_POR_ESTRATEGIA[ESTRATEGIA_INVESTIMENTO]
        dados_resumo = [
            {"Parâmetro": "Estratégia", "Valor": nome_estrategia},
            {"Parâmetro": "Data de Referência", "Valor": datetime.now().strftime("%d/%m/%Y %H:%M")},
            {"Parâmetro": "", "Valor": ""},
            {"Parâmetro": "=== Pesos Ações (%) ===", "Valor": ""},
            {"Parâmetro": "Dividendos", "Valor": pesos_acoes["dividendos"]},
            {"Parâmetro": "Valuation (P/L + P/VP)", "Valor": pesos_acoes["valuation"]},
            {"Parâmetro": "Rentabilidade (ROE + Margens)", "Valor": pesos_acoes["rentabilidade"]},
            {"Parâmetro": "Endividamento (Dív/PL + Liq. Corrente)", "Valor": pesos_acoes["endividamento"]},
            {"Parâmetro": "Crescimento (Receita + Lucro)", "Valor": pesos_acoes["crescimento"]},
            {"Parâmetro": "Recomendação (Consenso + Upside)", "Valor": pesos_acoes["recomendacao"]},
            {"Parâmetro": "Risco (Beta + Var. 52s)", "Valor": pesos_acoes["risco"]},
            {"Parâmetro": f"Filtro: DY mínimo", "Valor": f"{filtros_acoes['dy_min']}%"},
            {"Parâmetro": f"Filtro: P/L mínimo", "Valor": filtros_acoes["pe_min"]},
            {"Parâmetro": "", "Valor": ""},
            {"Parâmetro": "=== Pesos FIIs (%) ===", "Valor": ""},
            {"Parâmetro": "Dividendos", "Valor": pesos_fiis["dividendos"]},
            {"Parâmetro": "Valuation (P/VP)", "Valor": pesos_fiis["valuation"]},
            {"Parâmetro": "Estabilidade (Oscilação 52s)", "Valor": pesos_fiis["estabilidade"]},
            {"Parâmetro": "Liquidez Diária", "Valor": pesos_fiis["liquidez"]},
            {"Parâmetro": "Momentum (Vs Média 200d)", "Valor": pesos_fiis["momentum"]},
            {"Parâmetro": f"Filtro: DY mínimo", "Valor": f"{filtros_fiis['dy_min']}%"},
            {"Parâmetro": f"Filtro: Liquidez mínima", "Valor": f"R$ {filtros_fiis['liquidez_min']:,.0f}"},
        ]
        df_resumo = pd.DataFrame(dados_resumo)
        df_resumo.to_excel(writer, sheet_name='Estratégia', index=False)
        logger.info(f"Resumo da estratégia '{nome_estrategia}' salvo na aba 'Estratégia'.")

        # Ajusta a largura das colunas para cada aba
        for sheet_name in writer.sheets:
            sheet = writer.sheets[sheet_name]
            for col in sheet.columns:
                max_length = 0
                column = col[0].column_letter
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = (max_length + 2)
                sheet.column_dimensions[column].width = adjusted_width

    logger.info(f"Relatório salvo com sucesso em '{filename}'.")


def enviar_email_gmail(relatorio_path):
    """Envia o relatório por e-mail usando o Gmail SMTP."""
    try:
        if not GMAIL_PASSWORD:
            raise ValueError("GMAIL_PASSWORD não configurado no ENV")
        
        msg = MIMEMultipart()
        estrategia = NOMES_ESTRATEGIAS[ESTRATEGIA_INVESTIMENTO]
        msg["Subject"] = f"📊 Relatório de Investimentos (Ações e FIIs) - {estrategia} - {datetime.now().strftime('%d/%m/%Y')}"
        msg["From"] = EMAIL_REMETENTE
        msg["To"] = ", ".join(EMAIL_DESTINATARIOS)
        msg.attach(MIMEText(f"Segue em anexo o relatório de análise de ações e FIIs.\nEstratégia: {estrategia}", "plain"))

        with open(relatorio_path, "rb") as file:
            part = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(relatorio_path)}")
            msg.attach(part)

        # Conecta ao servidor SMTP do Gmail
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_REMETENTE, GMAIL_PASSWORD)
            server.sendmail(EMAIL_REMETENTE, EMAIL_DESTINATARIOS, msg.as_string())
        
        logger.info("Email enviado com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao enviar email via Gmail: {e}")


def enviar_relatorio():
    """Função principal que orquestra a análise e o envio."""
    logger.info("Iniciando a geração do relatório...")
    df_acoes, df_fiis = analisar_ativos()

    tem_dados = not df_acoes.empty or not df_fiis.empty

    if tem_dados:
        filename = "relatorio_investimentos.xlsx"
        salvar_em_excel(df_acoes, df_fiis, filename)
        enviar_email_gmail(filename)
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
