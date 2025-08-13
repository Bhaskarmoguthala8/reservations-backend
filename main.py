import os
from enum import Enum
from datetime import date
from uuid import UUID
from typing import List, Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Response, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from httpx import AsyncClient, HTTPStatusError, Timeout, ReadTimeout, ConnectTimeout
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client, Client

# Email helpers (Resend)
from email_utils import send_reservation_received, send_status_change

# -------------------- env & clients --------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")             # service_role key for REST
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")   # anon key for Auth
if not SUPABASE_URL or not SUPABASE_KEY or not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_URL, SUPABASE_KEY, SUPABASE_ANON_KEY must be set in .env")

# Supabase auth client (uses anon key)
sb_auth: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# HTTP client with friendlier timeouts
HTTP_TIMEOUT = Timeout(connect=5.0, read=20.0, write=20.0, pool=None)
client = AsyncClient(timeout=HTTP_TIMEOUT)

# Headers (service_role; RLS disabled in your project)
WRITE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}
READ_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Accept": "application/json",
    "Range": "0-9999",  # avoids pagination surprises
}

# Rate limiting
limiter = Limiter(key_func=get_remote_address)

# -------------------- FastAPI app --------------------
app = FastAPI(title="Rambling House Reservations API")

# Add rate limiting middleware
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS - restrict to your domain in production
FRONTEND_ORIGINS = [
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "https://theramblinghouse.ie",
    "https://frontend-phi-gold-23.vercel.app/",
    "https://www.theramblinghouse.ie"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# Add SlowAPI middleware
app.add_middleware(SlowAPIMiddleware)

# -------------------- security helpers --------------------
bearer = HTTPBearer(auto_error=False)

async def require_auth(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    token = None
    
    # Debug: Log what we receive
    print(f"🔍 Auth check - Cookies: {request.cookies}")
    print(f"🔍 Auth check - Authorization header: {request.headers.get('authorization')}")
    
    # Try cookie first, then Authorization header
    if request.cookies.get("auth_token"):
        token = request.cookies.get("auth_token")
        print(f"🔍 Using cookie token: {token[:20]}...")
    elif credentials:
        token = credentials.credentials
        print(f"🔍 Using header token: {token[:20]}...")
    
    if not token:
        print("❌ No token found")
        raise HTTPException(status_code=401, detail="Missing authentication")
        
    try:
        res = sb_auth.auth.get_user(token)
        user = res.user
        if not user:
            print("❌ Invalid user from token")
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        print(f"✅ Auth successful for user: {user.email}")
        return {"id": user.id, "email": user.email}
    except Exception as e:
        print(f"❌ Auth error: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# -------------------- models (match your DB: TEXT for guests/time/status) --------------------
class StatusEnum(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"

class ReservationBase(BaseModel):
    name: str = Field(..., max_length=100)
    email: EmailStr
    phone: str
    guests: str                 # TEXT in DB
    date: date
    time: str                   # TEXT in DB
    occasion: Optional[str] = Field(None, max_length=100)
    special_requests: Optional[str] = Field(None, max_length=500)

class ReservationIn(ReservationBase):
    pass  # DB default sets status='pending'

class Reservation(ReservationBase):
    id: str
    status: str                 # TEXT in DB

# ---- auth models (for Swagger / frontend) ----
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserInfo(BaseModel):
    id: str
    email: EmailStr

class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    user: Optional[UserInfo] = None

# -------------------- auth endpoint --------------------
@app.post("/auth/login", response_model=LoginResponse, summary="Login with email & password")
@limiter.limit("10/minute")  # Rate limit login attempts
async def login(request: Request, payload: LoginRequest, response: Response):
    try:
        res = sb_auth.auth.sign_in_with_password(
            {"email": payload.email, "password": payload.password}
        )
        if not res.session or not res.session.access_token:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # Set httpOnly cookie
        print(f"🍪 Setting cookie with token: {res.session.access_token[:20]}...")
        response.set_cookie(
            key="auth_token",
            value=res.session.access_token,
            httponly=True,
            secure=True,  # Set to True in production with HTTPS
            samesite="lax",
            max_age=3600,  # 1 hour
            domain=None  # Allow cookie to work on localhost and 127.0.0.1
        )
        print(f"🍪 Cookie set in response headers: {response.headers}")
        
        return LoginResponse(
            access_token=res.session.access_token,
            token_type="bearer",
            user=UserInfo(id=res.user.id, email=res.user.email) if res.user else None,
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid email or password")

# -------------------- reservation endpoints --------------------
@app.post(
    "/reservations",
    response_model=Reservation,
    status_code=201,
    summary="Create a reservation",
)
@limiter.limit("5/minute")  # Rate limit reservation creation
async def create_reservation(request: Request, res: ReservationIn, background_tasks: BackgroundTasks):
    # Additional validation
    if not res.name.strip() or len(res.name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters")
    if not res.phone.strip() or len(res.phone.strip()) < 8:
        raise HTTPException(status_code=400, detail="Phone number must be at least 8 digits")
    if not res.guests or not res.guests.isdigit() or int(res.guests) < 1 or int(res.guests) > 20:
        raise HTTPException(status_code=400, detail="Guests must be between 1 and 20")
    payload = jsonable_encoder({**res.dict(), "status": StatusEnum.pending})
    try:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/reservations",
            params={"select": "*"},
            json=payload,
            headers=WRITE_HEADERS,
        )
        resp.raise_for_status()
    except (ReadTimeout, ConnectTimeout):
        raise HTTPException(status_code=504, detail="Timed out contacting database. Please try again.")
    except HTTPStatusError:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    if not data:
        raise HTTPException(500, "Empty response from Supabase")
    created = data[0]

    try:
        background_tasks.add_task(send_reservation_received, created)
    except Exception as e:
        print(f"Failed to queue reservation-received email: {e}")

    return created

@app.get(
    "/reservations/{email}",
    response_model=List[Reservation],
    summary="List reservations by email",
)
async def get_reservations(email: EmailStr):
    params = {
        "select": "*",
        "email": f"eq.{email}",
        "order": "date.asc,time.asc",
    }
    try:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/reservations",
            params=params,
            headers=READ_HEADERS,
        )
        resp.raise_for_status()
    except (ReadTimeout, ConnectTimeout):
        raise HTTPException(status_code=504, detail="Timed out contacting database.")
    except HTTPStatusError:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

@app.get(
    "/reservations/status/{status}",
    response_model=List[Reservation],
    summary="List reservations by status (auth required)",
    dependencies=[Depends(require_auth)],
)
async def get_by_status(status: StatusEnum):
    params = {
        "select": "*",
        "status": f"eq.{status.value}",
        "order": "date.asc,time.asc",
    }
    try:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/reservations",
            params=params,
            headers=READ_HEADERS,
        )
        resp.raise_for_status()
    except (ReadTimeout, ConnectTimeout):
        raise HTTPException(status_code=504, detail="Timed out contacting database.")
    except HTTPStatusError:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

@app.get(
    "/admin/reservations",
    response_model=List[Reservation],
    summary="List ALL reservations (auth required)",
    dependencies=[Depends(require_auth)],
)
async def list_all_reservations(status: Optional[StatusEnum] = None):
    params = {"select": "*", "order": "date.asc,time.asc"}
    if status:
        params["status"] = f"eq.{status.value}"
    try:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/reservations",
            params=params,
            headers=READ_HEADERS,
        )
        resp.raise_for_status()
    except (ReadTimeout, ConnectTimeout):
        raise HTTPException(status_code=504, detail="Timed out contacting database.")
    except HTTPStatusError:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

# ---- PATCH uses UUID and WHERE via params ----
class StatusUpdateBody(BaseModel):
    status: StatusEnum

@app.patch(
    "/reservations/{res_id}/status",
    response_model=Reservation,
    summary="Update reservation status (auth required)",
    dependencies=[Depends(require_auth)],
)
async def update_status(res_id: UUID, body: StatusUpdateBody, background_tasks: BackgroundTasks):
    payload = jsonable_encoder({"status": body.status})
    try:
        resp = await client.patch(
            f"{SUPABASE_URL}/rest/v1/reservations",
            params={"id": f"eq.{str(res_id)}", "select": "*"},
            json=payload,
            headers=WRITE_HEADERS,
        )
        resp.raise_for_status()
    except (ReadTimeout, ConnectTimeout):
        raise HTTPException(status_code=504, detail="Timed out contacting database.")
    except HTTPStatusError:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    if not data:
        raise HTTPException(404, f"Reservation {res_id} not found")

    updated = data[0]

    if body.status in (StatusEnum.confirmed, StatusEnum.cancelled):
        try:
            background_tasks.add_task(send_status_change, updated)
        except Exception as e:
            print(f"Failed to queue status-change email: {e}")

    return updated
# -------------------- subscribers --------------------
class SubscriberIn(BaseModel):
    email: EmailStr

@app.post(
    "/subscribe",
    status_code=201,
    summary="Add a new email subscriber"
)
@limiter.limit("5/minute")  # Rate limit newsletter subscription
async def add_subscriber(request: Request, sub: SubscriberIn):
    payload = jsonable_encoder({"email": sub.email})
    try:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/subscribers",
            params={"select": "*"},
            json=payload,
            headers=WRITE_HEADERS,
        )
        resp.raise_for_status()
    except HTTPStatusError as e:
        # Unique constraint: duplicate email
        if resp.status_code == 409:
            raise HTTPException(status_code=409, detail="Already subscribed")
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    if not data:
        raise HTTPException(500, "Failed to subscribe")
    return {"message": "Subscription successful", "subscriber": data[0]}

# Add logout endpoint
@app.post("/auth/logout", summary="Logout")
async def logout(response: Response):
    response.delete_cookie(key="auth_token", httponly=True, secure=False, samesite="lax")
    return {"message": "Logged out successfully"}

# Debug endpoint to check auth status
@app.get("/auth/check", summary="Check authentication status")
async def check_auth(request: Request):
    token = request.cookies.get("auth_token")
    if token:
        try:
            res = sb_auth.auth.get_user(token)
            return {"authenticated": True, "user": {"id": res.user.id, "email": res.user.email}}
        except Exception:
            return {"authenticated": False, "error": "Invalid token"}
    return {"authenticated": False, "error": "No token found"}

@app.on_event("shutdown")
async def shutdown_event():
    await client.aclose()
