from fastapi import APIRouter, status

from api.handlers import auth, documents

router = APIRouter(prefix="/v1")

router.add_api_route("/auth/register", auth.register, methods=["POST"], status_code=status.HTTP_201_CREATED)
router.add_api_route("/auth/login", auth.login, methods=["POST"])

router.add_api_route(
    "/documents",
    documents.list_documents,
    methods=["GET"],
    response_model=documents.DocumentListResponse,
)
router.add_api_route(
    "/documents/upload",
    documents.upload_documents,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
    response_model=documents.UploadResponse,
    openapi_extra=documents.UPLOAD_OPENAPI,
)
