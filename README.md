# Finance Action Bot

![Python](https://img.shields.io/badge/Python-3.9-blue) ![Coolify](https://img.shields.io/badge/Coolify-Deployed-brightgreen) ![Amazon SES](https://img.shields.io/badge/Amazon%20SES-Email-orange)

## Descrição
**Finance Action Bot** é um bot automatizado para análise dinâmica de ações listadas na B3 (Bolsa de Valores do Brasil). Ele filtra ações com base em critérios financeiros predefinidos e gera relatórios em Excel. O bot utiliza **Amazon SES** para enviar o relatório gerado por email.

O projeto é configurado para rodar em **Coolify** usando **Nixpacks** ou Docker.

---

## Recursos
- Análise de ações listadas na B3 com critérios configuráveis.
- Geração automática de relatórios em Excel.
- Envio do relatório por email usando Amazon SES.
- Agendamento diário para execução automática.

---

## Tecnologias Utilizadas
- **Python 3.9**
- **Investpy**: Para busca de ações na B3.
- **YFinance**: Para análise de métricas financeiras.
- **Pandas**: Manipulação e geração de dados.
- **Boto3**: Integração com Amazon SES.
- **Coolify**: Hospedagem e gerenciamento do bot.
- **Nixpacks**: Para deploy sem necessidade de Dockerfile.

---

## Pré-requisitos
1. Conta AWS configurada com acesso ao Amazon SES.
2. Python 3.9+ instalado localmente (opcional para desenvolvimento).
3. Coolify configurado para deploy (opcional).

---

## Instalação Local
1. Clone este repositório:
   ```bash
   git clone <URL_DO_REPOSITORIO>
   cd <NOME_DO_REPOSITORIO>
   ```

2. Crie um ambiente virtual:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

3. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure as variáveis de ambiente no arquivo `.env`:
   ```plaintext
   AWS_REGION=sa-east-1
   AWS_ACCESS_KEY_ID=<sua_access_key>
   AWS_SECRET_ACCESS_KEY=<sua_secret_key>
   EMAIL_REMETENTE=mailer@seu-dominio.com
   EMAIL_DESTINATARIO=destinatario@email.com
   ```

5. Execute o bot:
   ```bash
   python excel-bot.py
   ```

---

## Deploy com Coolify
1. Suba o código para um repositório Git.
2. Configure uma nova aplicação no Coolify:
   - Escolha **Nixpacks** como Build Pack.
   - Configure as variáveis de ambiente necessárias:
     - `AWS_REGION`
     - `AWS_ACCESS_KEY_ID`
     - `AWS_SECRET_ACCESS_KEY`
     - `EMAIL_REMETENTE`
     - `EMAIL_DESTINATARIO`
   - Defina o comando de inicialização:
     ```bash
     python seu_script.py
     ```
3. Clique em **Save and Deploy**.

---

## Exemplo de Relatório Gerado
Um exemplo de relatório gerado pelo bot:

| Ticker | Preço Atual (R$) | Dividend Yield (%) | Crescimento (%) | Beta | Retorno Anual (R$) |
|--------|------------------|--------------------|------------------|------|--------------------|
| PETR4  | 30.25           | 8.5               | 10.2             | 0.9  | 2.57               |
| VALE3  | 70.30           | 6.8               | 12.1             | 1.2  | 4.77               |

---

## Contribuições
Sinta-se à vontade para abrir **issues** ou enviar **pull requests** para melhorias no projeto.

---

## Licença
Este projeto é licenciado sob a [Licença MIT](LICENSE).
