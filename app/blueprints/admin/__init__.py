from flask import Blueprint

bp = Blueprint("admin", __name__, template_folder="../../templates/admin")

from app.blueprints.admin import routes  # noqa: E402, F401