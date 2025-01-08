# Use uma imagem base com Python
FROM python:3.9-slim

# Defina o diretório de trabalho no contêiner
WORKDIR /app

# Copie os arquivos do projeto para o contêiner
COPY . .

# Instale as dependências do projeto
RUN pip install --no-cache-dir -r requirements.txt

# Exponha a porta necessária (se necessário, aqui não há uso direto de portas)
EXPOSE 80

# Comando para iniciar o bot
CMD ["python", "excel-bot.py"]
