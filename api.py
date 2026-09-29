"""FastAPI REST API for the single-user Personal AI Assistant."""
from __future__ import annotations
import json,logging,os,time
from typing import Optional,Any
from urllib.parse import urlparse
from fastapi import FastAPI,File,HTTPException,UploadFile,Request
from fastapi.responses import FileResponse,StreamingResponse,HTMLResponse,JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel,Field
from agent import MODEL,VISION_MODEL,run_agent,stream_agent
from config import ALLOW_SHELL,FILE_ROOT,ensure_directories
from db import connect
from tools import init_db,recall_memories,remember_fact,load_history,list_sessions,set_chat_title,get_chat_title,delete_chat,remove_last_assistant,remove_last_turn,save_turn
from memory import init_semantic_store,index_document,search_rag,rag_source_status,delete_semantic_memory,delete_rag_source,remember_semantic
from multimodal import save_upload,image_data_url,ensure_local_file,delete_upload,list_uploads
from mcp_registry import registry_snapshot
from connectors import init_connectors_db,list_connectors,upsert_connector,delete_connector,update_connector_status
from preferences import init_preferences_db,get_preferences,update_preferences
from jobs import init_jobs_db,create_job,get_job,list_jobs,mark_failed,requeue_interrupted_jobs
from task_queue import enqueue_chat_job,recover_pending_jobs,active_job_count
from document_parser import extract_and_limit,is_supported_document
from auth import SESSION_COOKIE,SESSION_DAYS,init_auth_db,account_exists,setup_account,login as auth_login,get_account_for_session,logout as auth_logout,update_account,change_password,login_rate_limited,clear_login_rate_limit
logger=logging.getLogger(__name__)
CHAT_RATE_LIMIT=max(1,int(os.getenv("CHAT_RATE_LIMIT","30"))); CHAT_RATE_WINDOW=max(1,int(os.getenv("CHAT_RATE_WINDOW","60")))

def _client_key(request):
    return request.client.host if request.client else "unknown"

def _check_chat_rate_limit(key):
    from db import connect
    now=int(time.time()); window=now//CHAT_RATE_WINDOW; bucket=f"chat:{key}:{window}"
    with connect() as con:
        row=con.execute(
            """INSERT INTO auth_login_attempts(bucket,attempts,updated_at) VALUES(?,?,?)
               ON CONFLICT(bucket) DO UPDATE SET attempts=auth_login_attempts.attempts+1,updated_at=excluded.updated_at
               RETURNING attempts""",(bucket,1,now)).fetchone()
    if row and int(row[0])>CHAT_RATE_LIMIT:
        raise HTTPException(status_code=429,detail="Too many chat requests. Try again later.")


ensure_directories()
if not os.getenv("DATABASE_URL","").strip(): raise RuntimeError("DATABASE_URL is required.")
init_db(); init_jobs_db(); requeue_interrupted_jobs(); init_semantic_store(); init_connectors_db(); init_preferences_db(); init_auth_db(); recover_pending_jobs()

app=FastAPI(title="Personal AI Assistant API",version="1.0.0",description="REST API for a single-user personal AI assistant.")
origins=[x.strip() for x in os.getenv("CORS_ORIGINS","http://localhost:3000,http://localhost:5173").split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"],allow_headers=["*"])
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        response=await call_next(request)
        response.headers.setdefault("X-Content-Type-Options","nosniff")
        response.headers.setdefault("X-Frame-Options","DENY")
        response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy","camera=(),microphone=(),geolocation=()")
        response.headers.setdefault("Content-Security-Policy","default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if request.url.scheme=="https" or os.getenv("FORCE_SECURE_COOKIES","1")=="1":
            response.headers.setdefault("Strict-Transport-Security","max-age=31536000; includeSubDomains")
        return response
app.add_middleware(SecurityHeadersMiddleware)

PUBLIC_API_PATHS={"/health","/api/v1/info","/api/v1/auth/status","/api/v1/auth/setup","/api/v1/auth/login","/api/v1/mcp/oauth/callback"}
@app.middleware("http")
async def authentication_middleware(request:Request,call_next):
    if request.method=="OPTIONS" or request.url.path in PUBLIC_API_PATHS or not request.url.path.startswith("/api/v1/"):
        return await call_next(request)
    account=get_account_for_session(request.cookies.get(SESSION_COOKIE))
    if not account:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail":"Authentication required."},status_code=401)
    if request.method in {"POST","PUT","PATCH","DELETE"}:
        origin=request.headers.get("origin")
        allowed={o.rstrip("/") for o in origins}
        if origin and origin.rstrip("/") not in allowed:
            return JSONResponse({"detail":"Cross-origin state-changing request blocked."},status_code=403)
        if not origin:
            referer=request.headers.get("referer")
            if referer:
                ref=urlparse(referer)
                ref_origin=f"{ref.scheme}://{ref.netloc}"
                if ref_origin.rstrip("/") not in allowed:
                    return JSONResponse({"detail":"Cross-origin state-changing request blocked."},status_code=403)
    request.state.account=account
    return await call_next(request)

class ChatRequest(BaseModel):
    message:str=Field(...,min_length=1,max_length=20000)
    session_id:str=Field(default="default",min_length=1,max_length=200)
    attachment_paths:list[str]=Field(default_factory=list,max_length=6)
    web_search:bool=True
    memory:bool=True
class ChatResponse(BaseModel):
    answer:str; session_id:str; model:str
class JobResponse(BaseModel):
    id:str; type:str; status:str; progress:int; message:Optional[str]=None
    result:Optional[str]=None; error:Optional[str]=None; session_id:Optional[str]=None
    user_message:Optional[str]=None; attempts:int=0
    created_at:Any=None; started_at:Any=None; completed_at:Any=None; updated_at:Any=None
class MemoryRequest(BaseModel):
    fact:str=Field(...,min_length=1,max_length=5000); source:str=Field(default="api",max_length=100)
class PreferencesRequest(BaseModel):
    appearance:Optional[str]=None; haptics:Optional[bool]=None; language:Optional[str]=None; web_search:Optional[bool]=None; memory:Optional[bool]=None
    custom_instructions:Optional[str]=Field(default=None,max_length=4000); response_style:Optional[str]=None
class MCPConnectorRequest(BaseModel):
    name:str=Field(...,min_length=1,max_length=80); transport:str="streamable-http"; url:str=Field(...,min_length=8,max_length=2000)
    allowed_tools:list[str]=Field(default_factory=list,max_length=100); headers:dict[str,str]=Field(default_factory=dict)
class AuthCredentials(BaseModel): email:str=Field(...,min_length=3,max_length=254); password:str=Field(...,min_length=8,max_length=200)
class AccountSetupRequest(AuthCredentials): display_name:str=Field(...,min_length=1,max_length=80)
class AccountUpdateRequest(BaseModel): display_name:str=Field(...,min_length=1,max_length=80); email:str=Field(...,min_length=3,max_length=254)
class PasswordChangeRequest(BaseModel): current_password:str=Field(...,min_length=8,max_length=200); new_password:str=Field(...,min_length=8,max_length=200)
class ChatTitleRequest(BaseModel): title:str=Field(...,min_length=1,max_length=80)
class RAGDocumentRequest(BaseModel):
    source:str=Field(...,min_length=1,max_length=500); content:str=Field(...,min_length=1,max_length=200000)

@app.get("/health")
def health(): return {"status":"ok","service":"personal-ai-assistant","model":MODEL,"shell_enabled":ALLOW_SHELL,"database":"postgresql","persistent_database":True,"background_jobs":True,"active_background_jobs":active_job_count()}
@app.get("/api/v1/info")
def info(): return {"name":"Personal AI Assistant","version":"1.0.0","model":MODEL,"shell_enabled":ALLOW_SHELL,"mode":"single-user"}

@app.get("/api/v1/auth/status")
def auth_status(request:Request):
    account=get_account_for_session(request.cookies.get(SESSION_COOKIE))
    return {"configured":account_exists(),"authenticated":bool(account),"account":account or None}

@app.post("/api/v1/auth/setup")
def auth_setup(request:AccountSetupRequest,raw_request:Request):
    try: account=setup_account(request.display_name,request.email,request.password)
    except RuntimeError as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
    except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc
    token,_=auth_login(request.email,request.password)
    from fastapi.responses import JSONResponse
    out=JSONResponse({"authenticated":True,"account":account})
    fwd=(raw_request.headers.get("x-forwarded-proto") or raw_request.headers.get("x-forwarded-protocol") or "").split(",")[0].strip().lower()
    public=os.getenv("PUBLIC_BASE_URL","").strip().lower()
    secure=(
        raw_request.url.scheme=="https"
        or fwd=="https"
        or public.startswith("https://")
        or os.getenv("FORCE_SECURE_COOKIES","1")=="1"
    )
    out.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="none" if secure else "lax",
        secure=secure,
        max_age=SESSION_DAYS*86400,
        path="/",
    )
    return out

@app.post("/api/v1/auth/login")
def auth_login_route(request:AuthCredentials,raw_request:Request):
    key=f"login:{_client_key(raw_request)}:{request.email.strip().lower()}"
    if login_rate_limited(key):
        raise HTTPException(status_code=429,detail="Too many login attempts. Try again later.")
    result=auth_login(request.email,request.password)
    if not result: raise HTTPException(status_code=401,detail="Invalid email or password.")
    clear_login_rate_limit(key)
    token,account=result
    from fastapi.responses import JSONResponse
    out=JSONResponse({"authenticated":True,"account":account})
    fwd=(raw_request.headers.get("x-forwarded-proto") or raw_request.headers.get("x-forwarded-protocol") or "").split(",")[0].strip().lower()
    public=os.getenv("PUBLIC_BASE_URL","").strip().lower()
    secure=(
        raw_request.url.scheme=="https"
        or fwd=="https"
        or public.startswith("https://")
        or os.getenv("FORCE_SECURE_COOKIES","1")=="1"
    )
    out.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="none" if secure else "lax",
        secure=secure,
        max_age=SESSION_DAYS*86400,
        path="/",
    )
    return out

@app.get("/api/v1/auth/me")
def auth_me(request:Request): return {"account":request.state.account}

@app.patch("/api/v1/auth/account")
def auth_account(request:AccountUpdateRequest,raw_request:Request):
    try:return {"account":update_account(raw_request.cookies.get(SESSION_COOKIE),request.display_name,request.email)}
    except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc

@app.post("/api/v1/auth/password")
def auth_password(request:PasswordChangeRequest,raw_request:Request):
    try: change_password(raw_request.cookies.get(SESSION_COOKIE),request.current_password,request.new_password); return {"updated":True}
    except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc

@app.post("/api/v1/auth/logout")
def auth_logout_route(request:Request):
    token=request.cookies.get(SESSION_COOKIE); auth_logout(token)
    from fastapi.responses import JSONResponse
    out=JSONResponse({"authenticated":False}); out.delete_cookie(SESSION_COOKIE,path="/"); return out

@app.get("/api/v1/settings")
def get_settings(): return {"settings":get_preferences()}
@app.patch("/api/v1/settings")
def patch_settings(request:PreferencesRequest):
    try:return {"settings":update_preferences(request.model_dump(exclude_none=True))}
    except ValueError as exc:raise HTTPException(status_code=400,detail=str(exc)) from exc
@app.post("/api/v1/settings/reset")
def reset_settings(): return {"settings":update_preferences({})}

@app.get("/api/v1/mcp/connectors")
def get_mcp_connectors(): return {"connectors":list_connectors(redact_headers=True)}
@app.post("/api/v1/mcp/connectors")
def add_mcp_connector(request:MCPConnectorRequest):
    try:
        connector=upsert_connector(
            request.name,
            request.transport,
            request.url,
            request.allowed_tools,
            request.headers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc

    try:
        from mcp_client import discover_connector_diagnostics
        status=discover_connector_diagnostics(connector["name"])
    except Exception as exc:
        status={"connected":False,"tools":0,"tool_names":[],"error":str(exc)[:500]}

    connector=update_connector_status(connector["id"],status) or connector
    connector["headers"]={k:"***" for k in connector.get("headers",{})}
    return {"connector":connector,"status":status}

@app.post("/api/v1/mcp/connectors/{connector_id}/test")
def test_mcp_connector(connector_id:int):
    connector=next((x for x in list_connectors() if x["id"]==connector_id),None)
    if not connector:
        raise HTTPException(status_code=404,detail="Connector not found.")
    try:
        from mcp_client import discover_connector_diagnostics
        status=discover_connector_diagnostics(connector["name"])
    except Exception as exc:
        status={"connected":False,"tools":0,"tool_names":[],"error":str(exc)[:500]}
    update_connector_status(connector_id,status)
    return {"ok":bool(status.get("connected")),**status}
@app.post("/api/v1/mcp/connectors/{connector_id}/oauth/start")
async def start_mcp_oauth(connector_id:int):
    connector=next((x for x in list_connectors() if x["id"]==connector_id),None)
    if not connector:raise HTTPException(status_code=404,detail="Connector not found.")
    try:
        from mcp_oauth import begin_oauth,oauth_redirect_uri
        return await begin_oauth(connector_id,oauth_redirect_uri())
    except Exception as exc: raise HTTPException(status_code=502,detail=str(exc)[:500]) from exc
@app.get("/api/v1/mcp/oauth/callback")
async def mcp_oauth_callback(code:str|None=None,state:str|None=None,iss:str|None=None,flow_id:str|None=None,error:str|None=None):
    if error:return HTMLResponse(f"<h2>MCP authorization failed</h2><p>{error[:300]}</p>",status_code=400)
    if not code or (not flow_id and not state):return HTMLResponse("<h2>MCP authorization failed</h2><p>Missing OAuth callback parameters.</p>",status_code=400)
    try:
        from mcp_oauth import complete_oauth
        await complete_oauth(flow_id or "",code,state,iss)
        return HTMLResponse("<h2>MCP connected</h2><p>You can return to Personal AI Assistant.</p><script>window.close()</script>")
    except Exception:
        return HTMLResponse("<h2>MCP authorization failed</h2><p>OAuth callback could not be completed.</p>",status_code=400)
@app.get("/api/v1/mcp/connectors/{connector_id}/oauth/status")
def get_mcp_oauth_status(connector_id:int):
    from mcp_oauth import oauth_status
    try:return oauth_status(connector_id)
    except ValueError as exc:raise HTTPException(status_code=404,detail=str(exc)) from exc
@app.delete("/api/v1/mcp/connectors/{connector_id}/oauth")
def remove_mcp_oauth(connector_id:int):
    from mcp_oauth import clear_oauth
    try:return {"deleted":clear_oauth(connector_id)}
    except ValueError as exc:raise HTTPException(status_code=404,detail=str(exc)) from exc
@app.delete("/api/v1/mcp/connectors/{connector_id}")
def remove_mcp_connector(connector_id:int):
    if not delete_connector(connector_id):raise HTTPException(status_code=404,detail="Connector not found.")
    return {"deleted":True,"id":connector_id}
@app.get("/api/v1/mcp/servers")
def get_mcp_servers():return {"servers":registry_snapshot()}

def _attachments(paths):
    image_urls=[]; rag_sources=[]
    for path in paths:
        try:image_urls.append(image_data_url(path))
        except ValueError:
            try:
                candidate=ensure_local_file(path)
                if not is_supported_document(candidate):raise ValueError("unsupported attachment type")
                rag_sources.append(str(candidate.relative_to(FILE_ROOT.resolve())))
            except (FileNotFoundError,PermissionError,ValueError) as exc:raise HTTPException(status_code=400,detail=f"Invalid document attachment: {path}") from exc
        except (FileNotFoundError,PermissionError) as exc:raise HTTPException(status_code=400,detail=f"Invalid image attachment: {path}") from exc
    return image_urls,rag_sources

def _public_job(job):
    if not job:
        return None
    payload = job.get("payload") or {}
    return {
        "id": job["id"],
        "type": job["type"],
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "result": job["result"] if job["status"] == "completed" else None,
        "error": job["error"] if job["status"] == "failed" else None,
        "session_id": payload.get("session_id"),
        "user_message": payload.get("message"),
        "attempts": job["attempts"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "updated_at": job["updated_at"],
    }


@app.post("/api/v1/chat/jobs",status_code=202)
def create_chat_job(request:ChatRequest,raw_request:Request):
    _check_chat_rate_limit("chat-job:"+_client_key(raw_request))
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(status_code=503,detail="GROQ_API_KEY is not configured.")

    # The free-tier runner executes in this same web service, so local
    # attachments are directly available while the process remains alive.
    if request.attachment_paths:
        for path in request.attachment_paths:
            try:
                ensure_local_file(path)
            except (FileNotFoundError,PermissionError):
                raise HTTPException(status_code=400,detail=f"Invalid attachment: {path}")

    job = create_job(
        "chat",
        {
            "message":request.message,
            "session_id":request.session_id,
            "attachment_paths":request.attachment_paths,
            "memory":request.memory,
            "web_search":request.web_search,
        },
    )
    try:
        enqueue_chat_job(job["id"])
    except Exception as exc:
        mark_failed(job["id"],str(exc))
        raise HTTPException(status_code=503,detail="Could not start background job.") from exc

    return _public_job(get_job(job["id"]))


@app.get("/api/v1/jobs",response_model=list[JobResponse])
def get_jobs(status:str|None=None,limit:int=50):
    recover_pending_jobs()
    statuses=[x.strip() for x in status.split(",")] if status else None
    return [_public_job(job) for job in list_jobs(statuses=statuses,limit=limit)]


@app.get("/api/v1/jobs/{job_id}",response_model=JobResponse)
def get_job_status(job_id:str):
    recover_pending_jobs()
    job=get_job(job_id)
    if not job:
        raise HTTPException(status_code=404,detail="Job not found.")
    return _public_job(job)


@app.post("/api/v1/chat",response_model=ChatResponse)
def chat(request:ChatRequest):
    if not os.getenv("GROQ_API_KEY"):raise HTTPException(status_code=503,detail="GROQ_API_KEY is not configured.")
    images,sources=_attachments(request.attachment_paths)
    try:answer=run_agent(request.message,session_id=request.session_id,image_urls=images,rag_sources=sources or None,memory_enabled=request.memory,web_search_enabled=request.web_search,verbose=False)
    except Exception as exc:logger.exception("Agent execution failed");raise HTTPException(status_code=500,detail="Agent execution failed.") from exc
    if answer.startswith("❌"):raise HTTPException(status_code=502,detail=answer)
    return ChatResponse(answer=answer,session_id=request.session_id,model=VISION_MODEL if images else MODEL)

@app.post("/api/v1/chat/stream")
def chat_stream(request:ChatRequest,raw_request:Request):
    _check_chat_rate_limit("chat:"+_client_key(raw_request))
    if not os.getenv("GROQ_API_KEY"):raise HTTPException(status_code=503,detail="GROQ_API_KEY is not configured.")
    images,sources=_attachments(request.attachment_paths)
    def event_stream():
        yield "event: start\ndata: "+json.dumps({"session_id":request.session_id})+"\n\n"
        try:
            for chunk in stream_agent(request.message,session_id=request.session_id,image_urls=images,rag_sources=sources or None,memory_enabled=request.memory,web_search_enabled=request.web_search):
                yield "event: delta\ndata: "+json.dumps({"text":chunk},ensure_ascii=False)+"\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception:
            logger.exception("Streaming request failed"); yield "event: error\ndata: "+json.dumps({"detail":"Streaming request failed."})+"\n\n"
    return StreamingResponse(event_stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no"})

@app.get("/api/v1/chats")
def list_chats(limit:int=50):
    limit=max(1,min(limit,100)); chats=[]
    for session in list_sessions(limit=limit):
        history=load_history(session,limit=4); title=get_chat_title(session) or next((m["content"] for m in history if m["role"]=="user"),"New conversation")
        chats.append({"session_id":session,"title":title[:80],"messages":history})
    return {"chats":chats}
@app.get("/api/v1/chats/{session_id}")
def get_chat(session_id:str):
    history=load_history(session_id,limit=100)
    if not history:raise HTTPException(status_code=404,detail="Chat not found.")
    return {"session_id":session_id,"title":get_chat_title(session_id) or next((m["content"] for m in history if m["role"]=="user"),"New conversation"),"messages":history}
@app.patch("/api/v1/chats/{session_id}")
def rename_chat(session_id:str,request:ChatTitleRequest):
    if not load_history(session_id,limit=1):raise HTTPException(status_code=404,detail="Chat not found.")
    set_chat_title(session_id,request.title); return {"session_id":session_id,"title":request.title.strip()}
@app.delete("/api/v1/chats/{session_id}")
def remove_chat(session_id:str):
    if not load_history(session_id,limit=1):raise HTTPException(status_code=404,detail="Chat not found.")
    delete_chat(session_id); return {"deleted":True,"session_id":session_id}
@app.delete("/api/v1/chats")
def remove_all_chats():
    sessions=list_sessions(limit=1000)
    for session in sessions:delete_chat(session)
    return {"deleted":len(sessions)}
@app.post("/api/v1/chats/{session_id}/regenerate")
def regenerate_chat(session_id:str):
    history=load_history(session_id,limit=100)
    if not history or history[-1]["role"]!="assistant":raise HTTPException(status_code=400,detail="No assistant response available to regenerate.")
    remove_last_assistant(session_id); history=load_history(session_id,limit=100)
    user_message=next((m["content"] for m in reversed(history) if m["role"]=="user"),None)
    if not user_message:raise HTTPException(status_code=400,detail="No user message available.")
    answer=run_agent(user_message,session_id=session_id)
    return {"answer":answer,"session_id":session_id}
@app.post("/api/v1/chats/{session_id}/edit")
def edit_chat(session_id:str,request:ChatRequest):
    history=load_history(session_id,limit=100)
    if not history:raise HTTPException(status_code=404,detail="Chat not found.")
    remove_last_turn(session_id); answer=run_agent(request.message,session_id=session_id)
    return {"answer":answer,"session_id":session_id}

@app.post("/api/v1/memories")
def create_memory(request:MemoryRequest):
    try:
        remember_fact(request.fact,source=request.source); remember_semantic(request.fact,source=request.source)
        return {"saved":True,"fact":request.fact.strip()}
    except Exception as exc:raise HTTPException(status_code=500,detail=str(exc)) from exc
@app.delete("/api/v1/memories")
def delete_memory(fact:str):
    exact=fact.strip()
    deleted_exact=delete_semantic_memory(exact)
    with connect() as con:
        cur=con.execute("DELETE FROM memories WHERE fact=?",(exact,))
    return {"deleted":bool(cur.rowcount or deleted_exact),"fact":exact}
@app.get("/api/v1/memories")
def list_memories(limit:int=50):return {"memories":recall_memories("",limit=max(1,min(limit,100)))}
@app.get("/api/v1/memories/search")
def search_memories(q:str,limit:int=8):
    if not q.strip():raise HTTPException(status_code=400,detail="Query cannot be empty.")
    from memory import search_semantic_memories
    return {"query":q,"memories":search_semantic_memories(q,limit=max(1,min(limit,50)))}

@app.post("/api/v1/rag/documents")
def create_rag_document(request:RAGDocumentRequest):
    try:return {"source":request.source,"chunks_added":index_document(request.source,request.content)}
    except Exception as exc:raise HTTPException(status_code=500,detail=str(exc)) from exc
@app.post("/api/v1/rag/files")
def create_rag_file(path:str):
    try:
        candidate=ensure_local_file(path)
        if not is_supported_document(candidate):raise ValueError("Unsupported document type.")
        added=index_document(path,extract_and_limit(candidate),replace_source=True); return {"path":path,"chunks_added":added}
    except FileNotFoundError:raise HTTPException(status_code=404,detail="File not found.")
    except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc))
    except Exception as exc:raise HTTPException(status_code=500,detail=str(exc)) from exc
@app.get("/api/v1/rag/sources")
def get_rag_sources():return {"sources":rag_source_status()}

MAX_UPLOAD_BYTES=10*1024*1024
@app.post("/api/v1/files/upload")
async def upload_file(file:UploadFile=File(...)):
    if not file.filename:raise HTTPException(status_code=400,detail="Filename is required.")
    content=await file.read(MAX_UPLOAD_BYTES+1)
    if len(content)>MAX_UPLOAD_BYTES:raise HTTPException(status_code=413,detail="File exceeds the 10 MB upload limit.")
    try:
        path=save_upload(file.filename,content); indexed=0; candidate=ensure_local_file(path)
        if is_supported_document(candidate):
            try:indexed=index_document(path,extract_and_limit(candidate),replace_source=True)
            except RuntimeError:pass
        return {"path":path,"name":candidate.name,"size":len(content),"indexed_chunks":indexed}
    except ValueError as exc:raise HTTPException(status_code=400,detail=str(exc)) from exc
@app.get("/api/v1/files")
def get_files():
    files=list_uploads(); statuses={x["source"]:x for x in rag_source_status()}
    for item in files:
        status=statuses.get(item["path"]); item["rag_indexed"]=bool(status); item["rag_chunks"]=status["chunks"] if status else 0; item["rag_indexed_at"]=status["indexed_at"] if status else None
    return {"files":files}
@app.get("/api/v1/files/{path:path}")
def get_file(path:str):
    try:candidate=ensure_local_file(path)
    except (FileNotFoundError,PermissionError):raise HTTPException(status_code=404,detail="File not found.")
    import mimetypes
    return FileResponse(candidate,media_type=mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",filename=candidate.name)
@app.delete("/api/v1/files/{path:path}")
def delete_file(path:str):
    try:delete_upload(path); deleted_chunks=delete_rag_source(path); return {"deleted":True,"path":path,"rag_chunks_deleted":deleted_chunks}
    except (FileNotFoundError,PermissionError):raise HTTPException(status_code=404,detail="File not found.")
