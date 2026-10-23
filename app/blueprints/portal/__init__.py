from flask import Blueprint

bp = Blueprint("portal", __name__, template_folder="../../templates/portal")

from app.blueprints.portal import routes  # noqa: E402, F401