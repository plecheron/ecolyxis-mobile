from flask import Blueprint

workspace_bp = Blueprint('workspace', __name__, url_prefix='/workspaces')

from . import routes  # noqa
