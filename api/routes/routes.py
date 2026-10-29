from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/v1")


@router.post("/auth/register")
async def register() -> None:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.post("/auth/login")
async def login() -> None:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")
