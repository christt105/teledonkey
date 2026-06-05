FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py mldonkey.py formatting.py ./

# Run unbuffered so logs show up immediately in `docker logs`.
CMD ["python", "-u", "bot.py"]
