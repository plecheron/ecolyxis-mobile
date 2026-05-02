from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>Ecolyxis</h1><p>It works!</p>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
