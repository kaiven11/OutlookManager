import asyncio
import json
import logging
import email
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import time
from functools import lru_cache

import httpx
from fastapi import FastAPI, HTTPException, Query, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from email.header import decode_header
from email.utils import parsedate_to_datetime
import imaplib
from fastapi.middleware.cors import CORSMiddleware
from itertools import groupby

# 导入配置模块
from config import verify_admin_password, get_config_info



# ============================================================================
# 配置常量
# ============================================================================

ACCOUNTS_FILE = "accounts.json"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SERVER = "outlook.live.com"
IMAP_PORT = 993

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型 (Pydantic)
# ============================================================================

class AccountCredentials(BaseModel):
    email: EmailStr
    refresh_token: str
    client_id: str

class AccountStatus(BaseModel):
    email: EmailStr
    status: str = "unknown"  # "active", "inactive", "unknown"

class AccountDeleteRequest(BaseModel):
    emails: List[EmailStr]

class AccountVerificationRequest(BaseModel):
    accounts: List[AccountCredentials]

class AccountVerificationResult(BaseModel):
    email: EmailStr
    status: str  # "success" 或 "error"
    message: str = ""
    credentials: Optional[AccountCredentials] = None

class EmailItem(BaseModel):
    message_id: str
    folder: str
    subject: str
    from_email: str
    date: str
    is_read: bool = False
    has_attachments: bool = False
    sender_initial: str = "?"

class EmailListResponse(BaseModel):
    email_id: str
    folder_view: str
    page: int
    page_size: int
    total_emails: int
    emails: List[EmailItem]

class DualViewEmailResponse(BaseModel):
    email_id: str
    inbox_emails: List[EmailItem]
    junk_emails: List[EmailItem]
    inbox_total: int
    junk_total: int

class EmailDetailsResponse(BaseModel):
    message_id: str
    subject: str
    from_email: str
    to_email: str
    date: str
    body_plain: Optional[str] = None
    body_html: Optional[str] = None

class AccountResponse(BaseModel):
    email_id: str
    message: str

# 简化的认证模型已移除，直接使用Bearer密码验证


# ============================================================================
# 极简认证函数
# ============================================================================

security = HTTPBearer()

def verify_admin_bearer(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """验证Bearer密码（极简认证）"""
    if not verify_admin_password(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True

async def get_current_admin(authenticated: bool = Depends(verify_admin_bearer)):
    """获取当前管理员用户（依赖注入）"""
    return authenticated

# ============================================================================
# 辅助函数
# ============================================================================

def decode_header_value(header_value: str) -> str:
    """解码邮件头字段"""
    if not header_value:
        return ""
    
    try:
        decoded_parts = decode_header(str(header_value))
        decoded_string = ""
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    decoded_string += part.decode(charset if charset else 'utf-8', 'replace')
                except (LookupError, UnicodeDecodeError):
                    decoded_string += part.decode('utf-8', 'replace')
            else:
                decoded_string += str(part)
        return decoded_string
    except Exception:
        return str(header_value) if header_value else ""


def extract_email_content(email_message: email.message.EmailMessage) -> tuple[str, str]:
    """提取邮件的纯文本和HTML内容"""
    body_plain = ""
    body_html = ""
    
    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            
            if 'attachment' not in content_disposition.lower():
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    payload = part.get_payload(decode=True)
                    if payload:
                        decoded_content = payload.decode(charset, errors='replace')
                        
                        if content_type == 'text/plain' and not body_plain:
                            body_plain = decoded_content
                        elif content_type == 'text/html' and not body_html:
                            body_html = decoded_content
                except Exception as e:
                    logger.warning(f"Failed to decode email part: {e}")
    else:
        try:
            charset = email_message.get_content_charset() or 'utf-8'
            payload = email_message.get_payload(decode=True)
            if payload:
                content = payload.decode(charset, errors='replace')
                content_type = email_message.get_content_type()
                
                if content_type == 'text/plain':
                    body_plain = content
                elif content_type == 'text/html':
                    body_html = content
                else:
                    body_plain = content  # 默认当作纯文本处理
        except Exception as e:
            logger.warning(f"Failed to decode email body: {e}")
    
    return body_plain, body_html


# ============================================================================
# 凭证管理模块
# ============================================================================

async def get_account_credentials(email_id: str) -> AccountCredentials:
    """从accounts.json获取账户凭证"""
    try:
        if not Path(ACCOUNTS_FILE).exists():
            raise HTTPException(status_code=404, detail="Account not found")
        
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        
        if email_id not in accounts:
            raise HTTPException(status_code=404, detail="Account not found")
        
        account_data = accounts[email_id]
        return AccountCredentials(
            email=email_id,
            refresh_token=account_data['refresh_token'],
            client_id=account_data['client_id']
        )
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to read accounts file")
    except Exception as e:
        logger.error(f"Error getting account credentials: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_all_accounts() -> Dict[str, Dict[str, str]]:
    """获取所有账户信息（优化IO）"""
    try:
        if not Path(ACCOUNTS_FILE).exists():
            return {}
        
        # 使用线程池执行文件读取
        return await asyncio.to_thread(_read_accounts_sync)
    except Exception as e:
        logger.error(f"Error getting all accounts: {e}")
        return {}

def _read_accounts_sync() -> Dict[str, Dict[str, str]]:
    """同步读取函数"""
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.error("Failed to decode accounts file")
        return {}


async def save_account_credentials(email_id: str, credentials: AccountCredentials) -> None:
    """保存账户凭证到accounts.json（优化IO操作）"""
    try:
        # 使用线程池执行文件IO操作
        await asyncio.to_thread(_save_account_sync, email_id, credentials)
        logger.info(f"Account credentials saved for {email_id}")
    except Exception as e:
        logger.error(f"Error saving account credentials: {e}")
        raise HTTPException(status_code=500, detail="Failed to save account")

def _save_account_sync(email_id: str, credentials: AccountCredentials):
    """同步保存函数"""
    accounts = {}
    if Path(ACCOUNTS_FILE).exists():
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
    
    accounts[email_id] = {
        'refresh_token': credentials.refresh_token,
        'client_id': credentials.client_id
    }
    
    # 原子写入，先写临时文件再移动
    temp_file = ACCOUNTS_FILE + '.tmp'
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    
    import os
    os.replace(temp_file, ACCOUNTS_FILE)


async def delete_accounts(emails: List[str]) -> Dict[str, int]:
    """从accounts.json中删除指定的账户"""
    try:
        if not Path(ACCOUNTS_FILE).exists():
            return {"deleted": 0, "not_found": len(emails)}
        
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        
        deleted = 0
        not_found = 0
        
        for email in emails:
            if email in accounts:
                del accounts[email]
                deleted += 1
            else:
                not_found += 1
        
        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Deleted {deleted} accounts, {not_found} not found")
        return {"deleted": deleted, "not_found": not_found}
    except json.JSONDecodeError:
        logger.error("Failed to decode accounts file")
        raise HTTPException(status_code=500, detail="Failed to read accounts file")
    except Exception as e:
        logger.error(f"Error deleting accounts: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete accounts")


# ============================================================================
# OAuth2令牌获取模块
# ============================================================================

async def get_access_token(credentials: AccountCredentials, check_only: bool = False) -> Optional[str]:
    """使用refresh_token获取access_token
    
    Args:
        credentials: 账户凭证
        check_only: 如果为True，验证失败时返回None而不是抛出异常
        
    Returns:
        成功返回access_token，如果check_only=True且验证失败则返回None
    """
    data = {
        'client_id': credentials.client_id,
        'grant_type': 'refresh_token',
        'refresh_token': credentials.refresh_token,
        'scope': 'https://outlook.office.com/IMAP.AccessAsUser.All offline_access'
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(TOKEN_URL, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            access_token = token_data.get('access_token')
            
            if not access_token:
                if check_only:
                    return None
                raise HTTPException(status_code=401, detail="Failed to obtain access token")
            
            logger.info(f"Successfully obtained access token for {credentials.email}")
            return access_token
    
    except httpx.HTTPError as e:
        logger.error(f"HTTP error getting access token: {e}")
        if check_only:
            return None
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception as e:
        logger.error(f"Error getting access token: {e}")
        if check_only:
            return None
        raise HTTPException(status_code=500, detail="Token acquisition failed")


# ============================================================================
# IMAP核心服务 - 邮件列表
# ============================================================================

class IMAPConnectionPool:
    """IMAP连接池，复用连接以提升性能"""
    def __init__(self):
        self._connections = {}
        self._lock = asyncio.Lock()
    
    async def get_connection(self, credentials: AccountCredentials, access_token: str):
        key = credentials.email
        async with self._lock:
            if key not in self._connections:
                imap_client = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
                auth_string = f"user={credentials.email}\1auth=Bearer {access_token}\1\1".encode('utf-8')
                imap_client.authenticate('XOAUTH2', lambda x: auth_string)
                self._connections[key] = imap_client
            return self._connections[key]
    
    def close_connection(self, email: str):
        if email in self._connections:
            try:
                self._connections[email].logout()
            except:
                pass
            del self._connections[email]

# 全局连接池实例
imap_pool = IMAPConnectionPool()

# 邮件列表缓存
class EmailCache:
    def __init__(self, ttl: int = 300):  # 5分钟缓存
        self.cache = {}
        self.ttl = ttl
    
    def get_key(self, email: str, folder: str, page: int, page_size: int) -> str:
        return f"{email}:{folder}:{page}:{page_size}"
    
    def get(self, email: str, folder: str, page: int, page_size: int) -> Optional[EmailListResponse]:
        key = self.get_key(email, folder, page, page_size)
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return data
            else:
                del self.cache[key]
        return None
    
    def set(self, email: str, folder: str, page: int, page_size: int, data: EmailListResponse):
        key = self.get_key(email, folder, page, page_size)
        self.cache[key] = (data, time.time())
    
    def clear_user(self, email: str):
        keys_to_remove = [k for k in self.cache.keys() if k.startswith(f"{email}:")]
        for key in keys_to_remove:
            del self.cache[key]

email_cache = EmailCache()

async def list_emails(credentials: AccountCredentials, folder: str, page: int, page_size: int) -> EmailListResponse:
    """获取邮件列表（含缓存）"""
    # 先检查缓存
    cached_result = email_cache.get(credentials.email, folder, page, page_size)
    if cached_result:
        logger.info(f"Cache hit for {credentials.email}, folder: {folder}, page: {page}")
        return cached_result
    
    access_token = await get_access_token(credentials)
    
    def _sync_list_emails():
        try:
            # 建立IMAP连接
            imap_client = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            
            # XOAUTH2认证
            auth_string = f"user={credentials.email}\1auth=Bearer {access_token}\1\1".encode('utf-8')
            imap_client.authenticate('XOAUTH2', lambda x: auth_string)
            
            all_emails_data = []
            
            # 根据folder参数决定要获取的文件夹
            folders_to_check = []
            if folder == "inbox":
                folders_to_check = ["INBOX"]
            elif folder == "junk":
                folders_to_check = ["Junk"]
            else:  # folder == "all"
                folders_to_check = ["INBOX", "Junk"]
            
            for folder_name in folders_to_check:
                try:
                    # 选择文件夹
                    imap_client.select(f'"{folder_name}"', readonly=True)
                    
                    # 搜索所有邮件
                    status, messages = imap_client.search(None, "ALL")
                    if status != 'OK' or not messages or not messages[0]:
                        continue
                        
                    message_ids = messages[0].split()
                    
                    # 按日期排序所需的数据（邮件ID和日期）
                    # 为了避免获取所有邮件的日期，我们假设ID顺序与日期大致相关
                    message_ids.reverse() # 通常ID越大越新
                    
                    for msg_id in message_ids:
                        all_emails_data.append({
                            "message_id_raw": msg_id,
                            "folder": folder_name
                        })

                except Exception as e:
                    logger.warning(f"Failed to access folder {folder_name}: {e}")
                    continue
            
            # 对所有文件夹的邮件进行统一分页
            total_emails = len(all_emails_data)
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            paginated_email_meta = all_emails_data[start_index:end_index]

            email_items = []
            # 按文件夹分组批量获取
            paginated_email_meta.sort(key=lambda x: x['folder'])
            
            for folder_name, group in groupby(paginated_email_meta, key=lambda x: x['folder']):
                try:
                    imap_client.select(f'"{folder_name}"', readonly=True)
                    
                    msg_ids_to_fetch = [item['message_id_raw'] for item in group]
                    if not msg_ids_to_fetch:
                        continue

                    # 批量获取邮件头（优化字段选择）
                    msg_id_sequence = b','.join(msg_ids_to_fetch)
                    status, msg_data = imap_client.fetch(msg_id_sequence, '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM)] FLAGS)')

                    if status != 'OK':
                        continue
                    
                    # 解析批量获取的数据
                    for i in range(0, len(msg_data), 2):
                        header_data = msg_data[i][1]
                        
                        # 从返回的原始数据中解析出msg_id
                        # e.g., b'1 (BODY[HEADER.FIELDS (SUBJECT DATE FROM)] {..}'
                        match = re.match(rb'(\d+)\s+\(', msg_data[i][0])
                        if not match:
                            continue
                        fetched_msg_id = match.group(1)

                        msg = email.message_from_bytes(header_data)
                        
                        # 优化字段解析，减少重复处理
                        subject = decode_header_value(msg.get('Subject')) or '(No Subject)'
                        from_email = decode_header_value(msg.get('From')) or '(Unknown Sender)'
                        date_str = msg.get('Date', '')
                        
                        # 优化日期解析，避免异常处理开销
                        if date_str:
                            try:
                                date_obj = parsedate_to_datetime(date_str)
                                formatted_date = date_obj.isoformat()
                            except:
                                formatted_date = datetime.now().isoformat()
                        else:
                            formatted_date = datetime.now().isoformat()
                        
                        message_id = f"{folder_name}-{fetched_msg_id.decode()}"
                        
                        # 提取发件人首字母
                        sender_initial = "?"
                        if from_email:
                            # 尝试提取邮箱用户名的首字母
                            email_match = re.search(r'([a-zA-Z])', from_email)
                            if email_match:
                                sender_initial = email_match.group(1).upper()
                        
                        email_item = EmailItem(
                            message_id=message_id,
                            folder=folder_name,
                            subject=subject,
                            from_email=from_email,
                            date=formatted_date,
                            is_read=False,  # 简化处理，实际可通过IMAP flags判断
                            has_attachments=False,  # 简化处理，实际需要检查邮件结构
                            sender_initial=sender_initial
                        )
                        email_items.append(email_item)

                except Exception as e:
                    logger.warning(f"Failed to fetch bulk emails from {folder_name}: {e}")
                    continue

            # 按日期重新排序最终结果
            email_items.sort(key=lambda x: x.date, reverse=True)

            # 关闭连接
            imap_client.logout()
            
            result = EmailListResponse(
                email_id=credentials.email,
                folder_view=folder,
                page=page,
                page_size=page_size,
                total_emails=total_emails,
                emails=email_items
            )
            
            # 缓存结果
            email_cache.set(credentials.email, folder, page, page_size, result)
            return result
        
        except Exception as e:
            logger.error(f"Error listing emails: {e}")
            if 'imap_client' in locals() and imap_client.state != 'LOGOUT':
                try:
                    imap_client.logout()
                except:
                    pass
            raise HTTPException(status_code=500, detail="Failed to retrieve emails")
    
    # 在线程池中运行同步代码
    return await asyncio.to_thread(_sync_list_emails)


# ============================================================================
# IMAP核心服务 - 邮件详情
# ============================================================================

async def get_email_details(credentials: AccountCredentials, message_id: str) -> EmailDetailsResponse:
    """获取邮件详细内容"""
    # 解析复合message_id
    try:
        folder_name, msg_id = message_id.split('-', 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message_id format")
    
    access_token = await get_access_token(credentials)
    
    def _sync_get_email_details():
        try:
            # 建立IMAP连接
            imap_client = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            
            # XOAUTH2认证 - 使用demo.py的方式
            auth_string = f"user={credentials.email}\1auth=Bearer {access_token}\1\1".encode('utf-8')
            imap_client.authenticate('XOAUTH2', lambda x: auth_string)
            
            # 选择正确的文件夹
            imap_client.select(folder_name)
            
            # 获取完整邮件内容
            status, msg_data = imap_client.fetch(msg_id, '(RFC822)')
            
            if status != 'OK' or not msg_data:
                raise HTTPException(status_code=404, detail="Email not found")
            
            # 解析邮件
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            # 提取基本信息
            subject = decode_header_value(msg.get('Subject', '(No Subject)'))
            from_email = decode_header_value(msg.get('From', '(Unknown Sender)'))
            to_email = decode_header_value(msg.get('To', '(Unknown Recipient)'))
            date_str = msg.get('Date', '')
            
            # 格式化日期
            try:
                if date_str:
                    date_obj = parsedate_to_datetime(date_str)
                    formatted_date = date_obj.isoformat()
                else:
                    formatted_date = datetime.now().isoformat()
            except:
                formatted_date = datetime.now().isoformat()
            
            # 提取邮件内容
            body_plain, body_html = extract_email_content(msg)
            
            # 关闭连接
            imap_client.logout()
            
            return EmailDetailsResponse(
                message_id=message_id,
                subject=subject,
                from_email=from_email,
                to_email=to_email,
                date=formatted_date,
                body_plain=body_plain if body_plain else None,
                body_html=body_html if body_html else None
            )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting email details: {e}")
            if 'imap_client' in locals():
                try:
                    imap_client.logout()
                except:
                    pass
            raise HTTPException(status_code=500, detail="Failed to retrieve email details")
    
    # 在线程池中运行同步代码
    return await asyncio.to_thread(_sync_get_email_details)


# ============================================================================
# FastAPI应用和API端点
# ============================================================================

app = FastAPI(
    title="Outlook邮件API服务",
    description="基于FastAPI和aioimaplib的异步邮件管理服务",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件服务
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/auth/config")
async def get_auth_config(current_admin: bool = Depends(get_current_admin)):
    """获取认证配置信息（用于前端判断认证状态）"""
    return get_config_info()

@app.post("/accounts", response_model=Union[AccountResponse, List[AccountResponse]])
async def register_account(
    credentials: Union[AccountCredentials, List[AccountCredentials]],
    current_admin: bool = Depends(get_current_admin)
):
    """注册或更新邮箱账户，支持单个或批量
    
    Args:
        credentials: 单个账户凭证或账户凭证列表
    
    Returns:
        单个账户响应或账户响应列表
    """
    # 处理单个凭证的情况
    if isinstance(credentials, AccountCredentials):
        return await register_single_account(credentials)
    
    # 处理凭证列表的情况
    results = []
    for cred in credentials:
        try:
            result = await register_single_account(cred)
            results.append(result)
        except HTTPException as e:
            # 对于批量操作，单个账户失败不应影响其他账户
            results.append(AccountResponse(
                email_id=cred.email,
                message=f"Error: {e.detail}"
            ))
    
    return results

async def register_single_account(credentials: AccountCredentials) -> AccountResponse:
    """注册或更新单个邮箱账户"""
    try:
        # 验证凭证有效性
        await get_access_token(credentials)
        
        # 保存凭证
        await save_account_credentials(credentials.email, credentials)
        
        return AccountResponse(
            email_id=credentials.email,
            message="Account verified and saved successfully."
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering account: {e}")
        raise HTTPException(status_code=500, detail="Account registration failed")

@app.get("/accounts", response_model=List[AccountStatus])
async def get_accounts(
    check_status: bool = False,
    current_admin: bool = Depends(get_current_admin)
):
    """获取所有账户列表，可选择检查账户活性状态
    
    Args:
        check_status: 是否检查账户活性状态
    """
    accounts = await get_all_accounts()
    
    if not check_status:
        # 仅返回邮箱列表，不检查状态
        return [AccountStatus(email=email) for email in accounts.keys()]
    
    # 并行检查所有账户的活性
    result = []
    tasks = []
    
    for email, account_data in accounts.items():
        credentials = AccountCredentials(
            email=email,
            refresh_token=account_data['refresh_token'],
            client_id=account_data['client_id']
        )
        # 创建异步任务
        task = asyncio.create_task(check_account_status(credentials))
        tasks.append((email, task))
    
    # 等待所有任务完成
    for email, task in tasks:
        is_active = await task
        status = "active" if is_active else "inactive"
        result.append(AccountStatus(email=email, status=status))
    
    return result

async def check_account_status(credentials: AccountCredentials) -> bool:
    """检查账户活性状态"""
    token = await get_access_token(credentials, check_only=True)
    return token is not None

@app.get("/emails/{email_id}", response_model=EmailListResponse)
async def get_emails(
    email_id: str,
    folder: str = Query("all", pattern="^(inbox|junk|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    current_admin: bool = Depends(get_current_admin)
):
    """获取邮件列表"""
    credentials = await get_account_credentials(email_id)
    return await list_emails(credentials, folder, page, page_size)


@app.get("/emails/{email_id}/dual-view")
async def get_dual_view_emails(
    email_id: str,
    inbox_page: int = Query(1, ge=1),
    junk_page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: bool = Depends(get_current_admin)
):
    """获取双栏视图邮件（收件箱和垃圾箱）"""
    credentials = await get_account_credentials(email_id)
    
    # 并行获取收件箱和垃圾箱邮件
    inbox_response = await list_emails(credentials, "inbox", inbox_page, page_size)
    junk_response = await list_emails(credentials, "junk", junk_page, page_size)
    
    return DualViewEmailResponse(
        email_id=email_id,
        inbox_emails=inbox_response.emails,
        junk_emails=junk_response.emails,
        inbox_total=inbox_response.total_emails,
        junk_total=junk_response.total_emails
    )


@app.get("/emails/{email_id}/{message_id}", response_model=EmailDetailsResponse)
async def get_email_detail(email_id: str, message_id: str, current_admin: bool = Depends(get_current_admin)):
    """获取邮件详细内容"""
    credentials = await get_account_credentials(email_id)
    return await get_email_details(credentials, message_id)


@app.get("/")
async def root():
    """根路径 - 返回前端页面"""
    return FileResponse("static/index.html")

@app.get("/api")
async def api_status():
    """API状态检查"""
    return {
        "message": "Outlook邮件API服务正在运行",
        "version": "1.0.0",
        "endpoints": {
            "register_account": "POST /accounts",
            "get_emails": "GET /emails/{email_id}",
            "get_email_detail": "GET /emails/{email_id}/{message_id}"
        }
    }

@app.post("/accounts/verify", response_model=List[AccountVerificationResult])
async def verify_accounts(
    request: AccountVerificationRequest,
    current_admin: bool = Depends(get_current_admin)
):
    """批量验证账户凭证有效性
    
    Args:
        request: 包含多个账户凭证的请求
    
    Returns:
        每个账户的验证结果列表
    """
    results = []
    tasks = []
    
    # 创建并行验证任务
    for credentials in request.accounts:
        task = asyncio.create_task(verify_single_account(credentials))
        tasks.append((credentials, task))
    
    # 等待所有任务完成
    for credentials, task in tasks:
        result = await task
        results.append(result)
    
    return results

async def verify_single_account(credentials: AccountCredentials) -> AccountVerificationResult:
    """验证单个账户凭证的有效性"""
    try:
        token = await get_access_token(credentials, check_only=True)
        if token:
            return AccountVerificationResult(
                email=credentials.email,
                status="success",
                message="账户验证成功",
                credentials=credentials
            )
        else:
            return AccountVerificationResult(
                email=credentials.email,
                status="error",
                message="获取访问令牌失败，请检查凭证"
            )
    except Exception as e:
        logger.error(f"Error verifying account {credentials.email}: {e}")
        return AccountVerificationResult(
            email=credentials.email,
            status="error",
            message=f"验证过程出错: {str(e)}"
        )

@app.delete("/accounts")
async def delete_multiple_accounts(
    request: AccountDeleteRequest,
    current_admin: bool = Depends(get_current_admin)
):
    """批量删除账户
    
    Args:
        request: 包含要删除的邮箱地址列表的请求
    
    Returns:
        删除操作的结果统计
    """
    if not request.emails:
        return {"message": "没有指定要删除的账户", "deleted": 0, "not_found": 0}
    
    result = await delete_accounts(request.emails)
    return {
        "message": f"成功删除 {result['deleted']} 个账户，{result['not_found']} 个账户未找到",
        **result
    }


# ============================================================================
# 启动配置
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 