from fastapi import APIRouter, status
from fastapi.responses import Response

from api.handlers import auth, chat, documents

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
router.add_api_route(
    "/documents/{doc_id}",
    documents.get_document,
    methods=["GET"],
    response_model=documents.DocumentSummary,
)
router.add_api_route(
    "/documents/{doc_id}/markdown",
    documents.get_document_markdown,
    methods=["GET"],
    response_class=Response,
    responses={200: {"content": {"text/markdown": {}}, "description": "The extracted Markdown"}},
)

router.add_api_route(
    "/chat/ticket",
    chat.create_chat_ticket,
    methods=["POST"],
    response_model=chat.ChatTicket,
)
router.add_api_websocket_route("/chat/ws/{business_id}", chat.chat_socket)
