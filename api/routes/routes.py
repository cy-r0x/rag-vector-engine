from fastapi import APIRouter, status

from api.handlers import auth

router = APIRouter(prefix="/v1")

router.add_api_route("/auth/register", auth.register, methods=["POST"], status_code=status.HTTP_201_CREATED)
router.add_api_route("/auth/login", auth.login, methods=["POST"])
