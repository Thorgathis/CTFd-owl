from flask import Flask, request
import os

app = Flask(__name__)

# Each linked task has its own dynamic flag, injected as FLAG<index>.
FLAGS = {
    1: os.getenv("FLAG1"),
    2: os.getenv("FLAG2"),
    3: os.getenv("FLAG3"),
}


@app.route("/")
def index():
    return "Send POST request to /flag/<n> to solve task n (1..3)!", 200


@app.route("/flag/<int:n>", methods=["GET", "POST"])
def flag(n):
    if request.method == "GET":
        return "Wrong method!", 400
    value = FLAGS.get(n)
    if value is None:
        return "No such task", 404
    return value, 200
