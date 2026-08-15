FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip3 install -r requirements.txt
COPY . .
EXPOSE 1337

CMD ["flask", "--app", "main", "run", "--port", "1337", "--host", "0.0.0.0"]
