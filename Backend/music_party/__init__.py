from flask import Flask, request

from .routes import api


def create_app():
    app = Flask(__name__)
    app.register_blueprint(api)

    @app.after_request
    def local_frontend_cors(response):
        origin = request.headers.get("Origin")
        if origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    return app
