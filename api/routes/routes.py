from fastapi import APIRouter

from api.handlers import auth

router = APIRouter(prefix="/v1")

router.add_api_route("/auth/register", auth.register, methods=["POST"])
router.add_api_route("/auth/login", auth.login, methods=["POST"])
