
from flask import Flask
from app.models import db

def create_app():

    app = Flask(__name__)

    app.config["SECRET_KEY"] = "change-this-development-key"

    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///campus.db"

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.route("/")
    def home():
        return "Welcome to Smart Campus Portal!"

    return app
