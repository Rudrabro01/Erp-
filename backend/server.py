import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Query, Body, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from mongomock_motor import AsyncMongoMockClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict
import uuid
from datetime import datetime, timezone, timedelta
from jose import jwt, JWTError
from passlib.context import CryptContext
import pandas as pd
import io
import zipfile
import base64
import re
from vision_ocr import detect_text
from twilio.rest import Client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncMongoMockClient()
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ.get('JWT_SECRET')
JWT_ALGORITHM = 'HS256'
JWT_EXPIRATION = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

GGSP_LOGO = "https://customer-assets.emergentagent.com/job_marks-portal-11/artifacts/h0m13o6i_GGSP-logo.png"
DEFAULT_COLLEGE = "Guru Gobind Singh Polytechnic"
DEFAULT_DEPT = "Nashik"

app = FastAPI(title="College ERP API")
api_router = APIRouter(prefix="/api")


# ===== Helpers =====
def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]


# ===== Models =====
class UserCreate(BaseModel):
    email: str
    password: str
    name: str
    role: str = "faculty"
    student_id: str = ""

class UserLogin(BaseModel):
    email: str
    password: str

class StudentCreate(BaseModel):
    roll_no: int
    name: str
    class_name: str
    contact: str = ""
    email: str = ""
    parent_phone: str = ""

class StudentUpdate(BaseModel):
    roll_no: Optional[int] = None
    name: Optional[str] = None
    class_name: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    parent_phone: Optional[str] = None

class SubjectCreate(BaseModel):
    name: str
    code: str
    class_name: str
    faculty_id: str = ""

class AttendanceBulk(BaseModel):
    date: str
    subject_id: str = ""
    entries: List[Dict[str, str]]

class MarksEntry(BaseModel):
    student_id: str
    subject_id: str
    score: float
    max_score: float = 100
    exam_type: str = "midterm"


# ===== JWT =====
def create_token(user_id: str, role: str):
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION)
    return jwt.encode({"sub": user_id, "role": role, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ===== Auth =====
@api_router.post("/auth/register")
async def register(data: UserCreate):
    if await db.users.find_one({"email": data.email}):
        raise HTTPException(status_code=400, detail="Email already exists")
    user_doc = {
        "id": str(uuid.uuid4()),
        "email": data.email,
        "password_hash": pwd_context.hash(data.password),
        "name": data.name,
        "role": data.role,
        "student_id": data.student_id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.users.insert_one(user_doc)
    token = create_token(user_doc["id"], user_doc["role"])
    safe = {k: v for k, v in user_doc.items() if k not in ("_id", "password_hash")}
    return {"token": token, "user": safe}

@api_router.post("/auth/login")
async def login(data: UserLogin):
    user = await db.users.find_one({"email": data.email}, {"_id": 0})
    if not user or not pwd_context.verify(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user["id"], user["role"])
    safe = {k: v for k, v in user.items() if k != "password_hash"}
    return {"token": token, "user": safe}

@api_router.get("/auth/me")
async def get_me(user=Depends(get_current_user)):
    return user


# ===== Students =====
@api_router.get("/students")
async def get_students(class_name: str = "", search: str = "", user=Depends(get_current_user)):
    query = {}
    if class_name:
        query["class_name"] = class_name
    if search:
        query["$or"] = [{"name": {"$regex": search, "$options": "i"}}]
        if search.isdigit():
            query["$or"].append({"roll_no": int(search)})
    students = await db.students.find(query, {"_id": 0}).sort("roll_no", 1).to_list(1000)
    return students

@api_router.post("/students")
async def create_student(data: StudentCreate, user=Depends(get_current_user)):
    if await db.students.find_one({"roll_no": data.roll_no}):
        raise HTTPException(status_code=400, detail="Roll number already exists")
    doc = {"id": str(uuid.uuid4()), **data.model_dump(), "created_at": datetime.now(timezone.utc).isoformat()}
    await db.students.insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}

@api_router.get("/students/{student_id}")
async def get_student(student_id: str, user=Depends(get_current_user)):
    student = await db.students.find_one({"id": student_id}, {"_id": 0})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return student

@api_router.put("/students/{student_id}")
async def update_student(student_id: str, data: StudentUpdate, user=Depends(get_current_user)):
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No data to update")
    result = await db.students.update_one({"id": student_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    return await db.students.find_one({"id": student_id}, {"_id": 0})

@api_router.delete("/students/{student_id}")
async def delete_student(student_id: str, user=Depends(require_admin)):
    result = await db.students.delete_one({"id": student_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    return {"message": "Student deleted"}

@api_router.post("/students/upload")
async def upload_students(file: UploadFile = File(...), user=Depends(get_current_user)):
    content = await file.read()
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading file: {str(e)}")
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    required = ['roll_no', 'name']
    for col in required:
        if col not in df.columns:
            raise HTTPException(status_code=400, detail=f"Missing column: {col}")
    added, skipped = 0, 0
    for _, row in df.iterrows():
        try:
            roll_no = int(row['roll_no'])
        except ValueError:
            continue
        if await db.students.find_one({"roll_no": roll_no}):
            skipped += 1
            continue
        doc = {
            "id": str(uuid.uuid4()), "roll_no": roll_no, "name": str(row['name']).strip(),
            "class_name": str(row.get('class_name', row.get('class', ''))).strip(),
            "contact": str(row.get('contact', '')).strip(),
            "email": str(row.get('email', '')).strip(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.students.insert_one(doc)
        added += 1
    return {"added": added, "skipped": skipped, "total": len(df)}


# ===== Subjects =====
@api_router.get("/subjects")
async def get_subjects(class_name: str = "", user=Depends(get_current_user)):
    query = {"class_name": class_name} if class_name else {}
    return await db.subjects.find(query, {"_id": 0}).to_list(100)

@api_router.post("/subjects")
async def create_subject(data: SubjectCreate, user=Depends(require_admin)):
    doc = {"id": str(uuid.uuid4()), **data.model_dump(), "created_at": datetime.now(timezone.utc).isoformat()}
    await db.subjects.insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}

@api_router.put("/subjects/{subject_id}")
async def update_subject(subject_id: str, data: dict = Body(...), user=Depends(require_admin)):
    data.pop("_id", None)
    data.pop("id", None)
    result = await db.subjects.update_one({"id": subject_id}, {"$set": data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Subject not found")
    return await db.subjects.find_one({"id": subject_id}, {"_id": 0})

@api_router.delete("/subjects/{subject_id}")
async def delete_subject(subject_id: str, user=Depends(require_admin)):
    result = await db.subjects.delete_one({"id": subject_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Subject not found")
    return {"message": "Subject deleted"}


TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "your_sid")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "your_token")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

def send_whatsapp(to_number: str, message: str):
    if TWILIO_ACCOUNT_SID == "your_sid" or not TWILIO_ACCOUNT_SID:
        logger.warning(f"Twilio credentials missing. MOCK WHATSAPP to {to_number}: {message}")
        return
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        # Ensure number starts with + and has whatsapp: prefix
        formatted_number = to_number.strip()
        if not formatted_number.startswith("+"):
            formatted_number = "+" + formatted_number
            
        res = client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{formatted_number}"
        )
        logger.info(f"REAL WHATSAPP SENT: {res.sid} to {formatted_number}")
    except Exception as e:
        logger.error(f"Failed to send Twilio WhatsApp message: {str(e)}")

async def process_attendance_alerts(student_ids: List[str]):
    try:
        settings = await db.settings.find_one({"id": "global"})
        limit = settings.get("attendance_alert_limit", 75)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for sid in student_ids:
            student = await db.students.find_one({"id": sid})
            if not student or not student.get("parent_phone"):
                continue
            if await db.alerts.find_one({"student_id": sid, "date": today, "type": "low_attendance"}):
                continue
            records = await db.attendance.find({"student_id": sid}).to_list(1000)
            if not records:
                continue
            present = sum(1 for r in records if r["status"] == "present")
            half_day = sum(1 for r in records if r["status"] == "half_day")
            pct = round((present + half_day * 0.5) / len(records) * 100, 1)
            if pct < limit:
                msg = f"Dear Parent, your child {student.get('name', 'Student')}'s attendance is {pct}%, which is below the required limit of {limit}%. Please take necessary action."
                send_whatsapp(student['parent_phone'], msg)
                await db.alerts.insert_one({
                    "id": str(uuid.uuid4()), "student_id": sid, "date": today,
                    "type": "low_attendance", "percentage": pct,
                    "created_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        logger.error(f"Error processing attendance alerts: {str(e)}")

# ===== Attendance =====
@api_router.post("/attendance")
async def mark_attendance(data: AttendanceBulk, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    marked = 0
    student_ids = []
    for entry in data.entries:
        student_ids.append(entry["student_id"])
        await db.attendance.update_one(
            {
                "student_id": entry["student_id"], 
                "date": data.date
            },
            {
                "$set": {
                    "status": entry["status"], 
                    "subject_id": data.subject_id,
                    "marked_by": user["id"]
                },
                "$setOnInsert": {
                    "id": str(uuid.uuid4()),
                    "student_id": entry["student_id"],
                    "date": data.date,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
            }, 
            upsert=True
        )
        marked += 1
    background_tasks.add_task(process_attendance_alerts, student_ids)
    return {"marked": marked}

@api_router.get("/attendance/daily")
async def get_daily_attendance(date: str, class_name: str = "", subject_id: str = "", user=Depends(get_current_user)):
    query = {"date": date}
    if subject_id:
        query["subject_id"] = subject_id
    records = await db.attendance.find(query, {"_id": 0}).to_list(10000)
    student_ids = [r["student_id"] for r in records]
    student_query = {"id": {"$in": student_ids}}
    if class_name:
        student_query["class_name"] = class_name
    students = await db.students.find(student_query, {"_id": 0}).to_list(10000)
    student_map = {s["id"]: s for s in students}
    filtered_records = []
    seen_students = set()
    
    # Process backwards so we predictably snag the absolute latest update if legacy duplicates exist
    for r in reversed(records):
        if r["student_id"] in seen_students:
            continue
        seen_students.add(r["student_id"])
        
        if r["student_id"] in student_map:
            r["student"] = student_map[r["student_id"]]
            filtered_records.append(r)
            
    filtered_records.sort(key=lambda x: x["student"].get("roll_no", 999999))
    present = sum(1 for r in filtered_records if r["status"] == "present")
    absent = sum(1 for r in filtered_records if r["status"] == "absent")
    half_day = sum(1 for r in filtered_records if r["status"] == "half_day")
    return {"records": filtered_records, "summary": {"present": present, "absent": absent, "half_day": half_day, "total": len(filtered_records)}}

@api_router.get("/report")
async def get_report_custom(class_name: str = "all", date: str = "", user=Depends(get_current_user)):
    print(f"DEBUG: get_report_custom invoked with -> class_name: {class_name}, date: {date}")
    
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    records = await db.attendance.find({"date": date}).to_list(10000)
    print(f"DEBUG: Found {len(records)} attendance records in DB for date {date}")
    
    student_ids = [r["student_id"] for r in records]
    
    student_query = {"id": {"$in": student_ids}}
    if class_name.lower() != "all" and class_name.strip() != "":
        student_query["class_name"] = class_name
        
    students = await db.students.find(student_query).to_list(10000)
    print(f"DEBUG: Found {len(students)} mapped students for class filter '{class_name}'")
    student_map = {s["id"]: s for s in students}
    
    present_list = []
    absent_list = []
    seen = set()
    
    for r in reversed(records):
        sid = r["student_id"]
        if sid in seen or sid not in student_map:
            continue
        seen.add(sid)
        
        student = student_map[sid]
        
        doc = {
            "rollNo": student.get("roll_no", ""),
            "name": student.get("name", ""),
            "class": student.get("class_name", ""),
            "date": date,
            "status": "Present" if r["status"] == "present" else "Absent" if r["status"] == "absent" else r["status"].capitalize()
        }
        
        if r["status"] == "present":
            present_list.append(doc)
        elif r["status"] == "absent":
            absent_list.append(doc)
            
    present_list.sort(key=lambda x: int(x["rollNo"]) if str(x["rollNo"]).isdigit() else 999999)
    absent_list.sort(key=lambda x: int(x["rollNo"]) if str(x["rollNo"]).isdigit() else 999999)
    
    payload = {"present": present_list, "absent": absent_list}
    print("DB RESULT: present =", len(present_list), ", absent =", len(absent_list))
    return payload

@api_router.get("/report/download")
async def download_report(date: str, class_name: str = ""):
    os.makedirs("reports", exist_ok=True)
    filename = f"attendance_report_{date}_{uuid.uuid4().hex[:6]}.pdf"
    file_path = f"reports/{filename}"
    
    try:
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(file_path)
        c.drawString(100, 800, "College ERP - Daily Attendance Report")
        c.drawString(100, 780, f"Date: {date}")
        if class_name:
            c.drawString(100, 760, f"Class: {class_name}")
        c.drawString(100, 740, "Total Present: See UI")
        c.save()
    except ImportError:
        # Fallback if reportlab missing
        with open(file_path, "wb") as f:
            f.write(b"%PDF-1.4\n%EOF\n")
            
    file_like = open(file_path, "rb")

    return StreamingResponse(
        file_like,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=attendance_report.pdf"
        }
    )

@api_router.get("/attendance/monthly")
async def get_monthly_attendance(from_date: str, to_date: str, class_name: str = "", subject_id: str = "", user=Depends(get_current_user)):
    query = {"date": {"$gte": from_date, "$lte": to_date}}
    if subject_id:
        query["subject_id"] = subject_id
    records = await db.attendance.find(query, {"_id": 0}).to_list(10000)
    student_query = {"class_name": class_name} if class_name else {}
    students = await db.students.find(student_query, {"_id": 0}).to_list(1000)
    student_map = {s["id"]: s for s in students}
    student_summary = {}
    for record in records:
        sid = record["student_id"]
        if sid not in student_map:
            continue
        if sid not in student_summary:
            student_summary[sid] = {"present": 0, "absent": 0, "half_day": 0, "dates": {}}
        student_summary[sid][record["status"]] += 1
        student_summary[sid]["dates"][record["date"]] = record["status"]
        
    result = []
    for sid, summary in student_summary.items():
        student = student_map[sid]
        total = summary["present"] + summary["absent"] + summary["half_day"]
        pct = round((summary["present"] + summary["half_day"] * 0.5) / total * 100, 1) if total > 0 else 0
        result.append({"student_id": sid, "student": student, "present": summary["present"], "absent": summary["absent"], "half_day": summary["half_day"], "total": total, "percentage": pct, "dates": summary["dates"]})
        
    result.sort(key=lambda x: x["student"].get("roll_no", 999999))
    return result

@api_router.get("/attendance/student/{student_id}")
async def get_student_attendance(student_id: str, from_date: str = "", to_date: str = "", user=Depends(get_current_user)):
    query = {"student_id": student_id}
    if from_date and to_date:
        query["date"] = {"$gte": from_date, "$lte": to_date}
    records = await db.attendance.find(query, {"_id": 0}).sort("date", 1).to_list(1000)
    present = sum(1 for r in records if r["status"] == "present")
    absent = sum(1 for r in records if r["status"] == "absent")
    half_day = sum(1 for r in records if r["status"] == "half_day")
    total = len(records)
    pct = round((present + half_day * 0.5) / total * 100, 1) if total > 0 else 0
    return {"records": records, "summary": {"present": present, "absent": absent, "half_day": half_day, "total": total, "percentage": pct}}


# ===== Marks =====
def categorize_marks(score, max_score, ranges):
    pct = (score / max_score) * 100 if max_score > 0 else 0
    if ranges:
        if pct >= ranges.get("L1", {}).get("min", 75):
            return "L1"
        elif pct >= ranges.get("L2", {}).get("min", 50):
            return "L2"
    else:
        if pct >= 75:
            return "L1"
        elif pct >= 50:
            return "L2"
    return "L3"

@api_router.post("/marks")
async def add_marks(data: MarksEntry, user=Depends(get_current_user)):
    settings = await db.settings.find_one({"id": "global"}, {"_id": 0})
    ranges = settings.get("marks_ranges", {}) if settings else {}
    category = categorize_marks(data.score, data.max_score, ranges)
    doc = {"id": str(uuid.uuid4()), **data.model_dump(), "category": category, "created_at": datetime.now(timezone.utc).isoformat()}
    await db.marks.update_one(
        {"student_id": data.student_id, "subject_id": data.subject_id, "exam_type": data.exam_type},
        {"$set": doc}, upsert=True
    )
    return {k: v for k, v in doc.items() if k != "_id"}

@api_router.post("/marks/upload")
async def upload_marks(subject_id: str = Query(...), exam_type: str = Query("midterm"), max_score: float = Query(100), file: UploadFile = File(...), user=Depends(get_current_user)):
    content = await file.read()
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error reading file: {str(e)}")
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    if 'roll_no' not in df.columns or 'score' not in df.columns:
        raise HTTPException(status_code=400, detail="File must have 'roll_no' and 'score' columns")
    settings = await db.settings.find_one({"id": "global"}, {"_id": 0})
    ranges = settings.get("marks_ranges", {}) if settings else {}
    added, errors = 0, []
    for _, row in df.iterrows():
        try:
            roll_no = int(row['roll_no'])
        except ValueError:
            continue
        student = await db.students.find_one({"roll_no": roll_no}, {"_id": 0})
        if not student:
            errors.append(f"Student {roll_no} not found")
            continue
        score = float(row['score'])
        category = categorize_marks(score, max_score, ranges)
        doc = {
            "id": str(uuid.uuid4()), "student_id": student["id"], "subject_id": subject_id,
            "score": score, "max_score": max_score, "exam_type": exam_type,
            "category": category, "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.marks.update_one(
            {"student_id": student["id"], "subject_id": subject_id, "exam_type": exam_type},
            {"$set": doc}, upsert=True
        )
        added += 1
    return {"added": added, "errors": errors}

@api_router.get("/marks/subject/{subject_id}")
async def get_subject_marks(subject_id: str, exam_type: str = "", level: str = "all", user=Depends(get_current_user)):
    query = {"subject_id": subject_id}
    if exam_type:
        query["exam_type"] = exam_type
    if level and level != "all":
        query["category"] = level
    marks = await db.marks.find(query, {"_id": 0}).to_list(1000)
    student_ids = [m["student_id"] for m in marks]
    students = await db.students.find({"id": {"$in": student_ids}}, {"_id": 0}).to_list(1000)
    smap = {s["id"]: s for s in students}
    for m in marks:
        m["student"] = smap.get(m["student_id"], {})
    l1 = sum(1 for m in marks if m.get("category") == "L1")
    l2 = sum(1 for m in marks if m.get("category") == "L2")
    l3 = sum(1 for m in marks if m.get("category") == "L3")
    avg = round(sum(m["score"] for m in marks) / len(marks), 1) if marks else 0
    return {"marks": marks, "summary": {"l1": l1, "l2": l2, "l3": l3, "average": avg, "total": len(marks)}}

@api_router.get("/marks/student/{student_id}")
async def get_student_marks(student_id: str, user=Depends(get_current_user)):
    marks = await db.marks.find({"student_id": student_id}, {"_id": 0}).to_list(100)
    subject_ids = list(set(m["subject_id"] for m in marks))
    subjects = await db.subjects.find({"id": {"$in": subject_ids}}, {"_id": 0}).to_list(100)
    smap = {s["id"]: s for s in subjects}
    for m in marks:
        m["subject"] = smap.get(m["subject_id"], {})
    return marks


# ===== Settings =====
@api_router.get("/settings")
async def get_settings():
    settings = await db.settings.find_one({"id": "global"}, {"_id": 0})
    if not settings:
        settings = {
            "id": "global", "college_name": DEFAULT_COLLEGE,
            "department_name": DEFAULT_DEPT, "logo_url": GGSP_LOGO,
            "splash_duration": 3,
            "theme": {"primary_color": "#4F5BD5", "secondary_color": "#0D9488", "dark_mode": False},
            "marks_ranges": {"L1": {"min": 75, "max": 100, "label": "Excellent"}, "L2": {"min": 50, "max": 74, "label": "Average"}, "L3": {"min": 0, "max": 49, "label": "Below Average"}}
        }
        await db.settings.insert_one({**settings})
    return settings

@api_router.put("/settings")
async def update_settings(data: dict = Body(...), user=Depends(require_admin)):
    data.pop("_id", None)
    data.pop("id", None)
    await db.settings.update_one({"id": "global"}, {"$set": data}, upsert=True)
    return await db.settings.find_one({"id": "global"}, {"_id": 0})


# ===== Stats =====
@api_router.get("/stats")
async def get_stats(user=Depends(get_current_user)):
    total_students = await db.students.count_documents({})
    total_subjects = await db.subjects.count_documents({})
    total_faculty = await db.users.count_documents({"role": "faculty"})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_att = await db.attendance.find({"date": today}, {"_id": 0}).to_list(10000)
    present = sum(1 for a in today_att if a["status"] == "present")
    absent = sum(1 for a in today_att if a["status"] == "absent")
    half_day = sum(1 for a in today_att if a["status"] == "half_day")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = await db.attendance.find({"date": {"$gte": week_ago}}, {"_id": 0}).to_list(10000)
    daily = {}
    for r in recent:
        d = r["date"]
        if d not in daily:
            daily[d] = {"present": 0, "absent": 0, "half_day": 0}
        daily[d][r["status"]] += 1
    trend = [{"date": d, **c} for d, c in sorted(daily.items())]
    pipeline = [{"$group": {"_id": "$class_name", "count": {"$sum": 1}}}]
    class_counts = await db.students.aggregate(pipeline).to_list(100)
    classes = [{"class_name": c["_id"], "count": c["count"]} for c in class_counts if c["_id"]]
    return {
        "total_students": total_students, "total_subjects": total_subjects, "total_faculty": total_faculty,
        "present_today": present, "absent_today": absent, "half_day_today": half_day,
        "attendance_trend": trend, "classes": classes
    }


# ===== Users =====
@api_router.get("/users")
async def get_users(user=Depends(require_admin)):
    return await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(100)

@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, user=Depends(require_admin)):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    result = await db.users.delete_one({"id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User deleted"}


# ===== Classes =====
@api_router.get("/classes")
async def get_classes(user=Depends(get_current_user)):
    pipeline = [{"$group": {"_id": "$class_name"}}, {"$sort": {"_id": 1}}]
    result = await db.students.aggregate(pipeline).to_list(100)
    return [r["_id"] for r in result if r["_id"]]


# ===== Admin: Reset Database =====
@api_router.post("/admin/reset-database")
async def reset_database(user=Depends(require_admin)):
    for col in ['students', 'attendance', 'marks', 'subjects', 'notifications']:
        await db[col].delete_many({})
    await db.users.delete_many({"role": {"$ne": "admin"}})
    return {"message": "Database reset successfully. Admin account preserved."}


# ===== Admin: Upload Logo =====
@api_router.post("/admin/upload-logo")
async def upload_logo(file: UploadFile = File(...), user=Depends(require_admin)):
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")
    b64 = base64.b64encode(content).decode('utf-8')
    mime = file.content_type or 'image/png'
    data_uri = f"data:{mime};base64,{b64}"
    await db.settings.update_one({"id": "global"}, {"$set": {"logo_url": data_uri}})
    return {"logo_url": data_uri}


# ===== Admin: Download Project =====
@api_router.get("/admin/download-project")
async def download_project(user=Depends(require_admin)):
    buffer = io.BytesIO()
    frontend_dir = Path('/app/frontend')
    backend_dir = Path('/app/backend')
    skip_dirs = {'node_modules', '.git', 'build', 'dist', '__pycache__', '.next'}

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in ['server.py']:
            p = backend_dir / f
            if p.exists():
                zf.write(p, f'backend/{f}')
        req = backend_dir / 'requirements.txt'
        if req.exists():
            zf.write(req, 'backend/requirements.txt')
        zf.writestr('backend/.env.example', 'MONGO_URL=mongodb://localhost:27017\nDB_NAME=college_erp\nJWT_SECRET=your-secret-key-change-this\nCORS_ORIGINS=*\n')

        for root, dirs, files in os.walk(frontend_dir / 'src'):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                fp = Path(root) / f
                arcname = 'frontend/' + str(fp.relative_to(frontend_dir))
                zf.write(fp, arcname)
        pub_dir = frontend_dir / 'public'
        if pub_dir.exists():
            for root, dirs, files in os.walk(pub_dir):
                for f in files:
                    fp = Path(root) / f
                    arcname = 'frontend/' + str(fp.relative_to(frontend_dir))
                    zf.write(fp, arcname)
        for f in ['package.json', 'tailwind.config.js', 'postcss.config.js', 'craco.config.js', 'jsconfig.json']:
            p = frontend_dir / f
            if p.exists():
                zf.write(p, f'frontend/{f}')
        zf.writestr('frontend/.env.example', 'REACT_APP_BACKEND_URL=http://localhost:8001\n')

        readme_content = f"""# {DEFAULT_COLLEGE} - ERP System

## Tech Stack
- **Frontend**: React.js + Tailwind CSS + Shadcn UI + Recharts + Framer Motion
- **Backend**: FastAPI (Python) + MongoDB
- **Auth**: JWT (HS256) with role-based access (Admin/Faculty/Student)

## Project Structure
```
college-erp/
├── backend/
│   ├── server.py          # FastAPI application (all routes)
│   ├── requirements.txt   # Python dependencies
│   └── .env.example       # Environment variables template
├── frontend/
│   ├── src/
│   │   ├── components/    # UI components (Shadcn + custom)
│   │   ├── contexts/      # React contexts (Auth, Theme)
│   │   ├── pages/         # Page components
│   │   ├── utils/         # Utilities (API, export helpers)
│   │   ├── App.js         # Main app with routing
│   │   └── index.js       # Entry point
│   ├── public/            # Static assets
│   ├── package.json       # Node.js dependencies
│   └── .env.example       # Environment variables template
└── README.md
```

## Setup Instructions

### Prerequisites
- Python 3.9+
- Node.js 16+
- MongoDB (running on localhost:27017)

### Backend Setup
```bash
cd backend
cp .env.example .env        # Edit .env with your settings
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

### Frontend Setup
```bash
cd frontend
cp .env.example .env        # Edit API URL if needed
npm install                  # or: yarn install
npm start                    # or: yarn start
```

### Environment Variables

**Backend (.env)**
| Variable | Description | Default |
|----------|-------------|---------|
| MONGO_URL | MongoDB connection string | mongodb://localhost:27017 |
| DB_NAME | Database name | college_erp |
| JWT_SECRET | JWT signing secret | (set your own) |
| CORS_ORIGINS | Allowed CORS origins | * |

**Frontend (.env)**
| Variable | Description | Default |
|----------|-------------|---------|
| REACT_APP_BACKEND_URL | Backend API URL | http://localhost:8001 |

## Default Admin Credentials
- **Email**: admin@abc.edu
- **Password**: admin123

> Change these credentials after first login!

## Features
- Splash Screen with college branding
- JWT Authentication (Admin/Faculty/Student roles)
- Dashboard with Recharts analytics
- Student Management (CRUD + CSV/Excel upload)
- Attendance System (Manual + OCR via Tesseract.js)
- Marks Management (L1/L2/L3 categorization)
- Monthly Reports with date-range filtering
- File Export (Excel, CSV, PDF)
- WhatsApp Sharing
- Dark/Light Mode
- Admin Panel (Branding, Subjects, Users, Settings)
"""
        zf.writestr('README.md', readme_content)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type='application/zip',
        headers={'Content-Disposition': 'attachment; filename=college-erp-project.zip'}
    )


# ===== Stats: Insights =====
@api_router.get("/stats/insights")
async def get_insights(class_name: str = "", subject_id: str = "", user=Depends(get_current_user)):
    all_marks = await db.marks.find({}, {"_id": 0}).to_list(10000)
    all_attendance = await db.attendance.find({}, {"_id": 0}).to_list(10000)
    students = await db.students.find({}, {"_id": 0}).to_list(1000)
    subjects_list = await db.subjects.find({}, {"_id": 0}).to_list(100)
    student_map = {s["id"]: s for s in students}
    subject_map = {s["id"]: s for s in subjects_list}

    if class_name:
        class_sids = {s["id"] for s in students if s.get("class_name") == class_name}
        all_marks = [m for m in all_marks if m["student_id"] in class_sids]
        all_attendance = [a for a in all_attendance if a["student_id"] in class_sids]
    if subject_id:
        all_marks = [m for m in all_marks if m.get("subject_id") == subject_id]
        all_attendance = [a for a in all_attendance if a.get("subject_id") == subject_id]

    highest_mark = max(all_marks, key=lambda m: m.get("score", 0), default=None)
    lowest_mark = min(all_marks, key=lambda m: m.get("score", float('inf')), default=None)

    att_by_student = {}
    for a in all_attendance:
        sid = a["student_id"]
        if sid not in att_by_student:
            att_by_student[sid] = {"present": 0, "absent": 0, "half_day": 0, "total": 0}
        att_by_student[sid][a["status"]] += 1
        att_by_student[sid]["total"] += 1

    best_att, worst_att = None, None
    for sid, data in att_by_student.items():
        pct = round((data["present"] + data["half_day"] * 0.5) / data["total"] * 100, 1) if data["total"] > 0 else 0
        entry = {"student": student_map.get(sid, {}), "percentage": pct, **data}
        if best_att is None or pct > best_att["percentage"]:
            best_att = entry
        if worst_att is None or pct < worst_att["percentage"]:
            worst_att = entry

    return {
        "highest_mark": {"student": student_map.get(highest_mark["student_id"], {}), "score": highest_mark["score"], "max_score": highest_mark.get("max_score", 100), "subject": subject_map.get(highest_mark.get("subject_id", ""), {})} if highest_mark else None,
        "lowest_mark": {"student": student_map.get(lowest_mark["student_id"], {}), "score": lowest_mark["score"], "max_score": lowest_mark.get("max_score", 100), "subject": subject_map.get(lowest_mark.get("subject_id", ""), {})} if lowest_mark else None,
        "best_attendance": best_att,
        "worst_attendance": worst_att,
    }


# ===== Admin: Seed Sample Data =====
@api_router.post("/admin/seed-sample-data")
async def seed_sample_data(user=Depends(require_admin)):
    import random
    classes = ["CO-A", "CO-B", "IF-A"]
    added_students = 0
    for i in range(1, 21):
        roll_no = i
        if not await db.students.find_one({"roll_no": roll_no}):
            await db.students.insert_one({
                "id": str(uuid.uuid4()), "roll_no": roll_no,
                "name": f"Student {i}", "class_name": random.choice(classes),
                "contact": f"98765{str(i).zfill(5)}", "email": f"student{i}@ggsp.edu",
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            added_students += 1

    sample_subjects = [
        {"name": "Data Structures", "code": "CS201", "class_name": "CO-A"},
        {"name": "Database Systems", "code": "CS301", "class_name": "CO-A"},
        {"name": "Operating Systems", "code": "CS302", "class_name": "CO-B"},
        {"name": "Computer Networks", "code": "CS401", "class_name": "IF-A"},
    ]
    subject_ids = []
    for sd in sample_subjects:
        existing = await db.subjects.find_one({"code": sd["code"]})
        if existing:
            subject_ids.append(existing.get("id", str(uuid.uuid4())))
        else:
            doc = {"id": str(uuid.uuid4()), **sd, "faculty_id": "", "created_at": datetime.now(timezone.utc).isoformat()}
            await db.subjects.insert_one(doc)
            subject_ids.append(doc["id"])

    all_students = await db.students.find({}, {"_id": 0}).to_list(100)
    for day_offset in range(7):
        d = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        for s in all_students:
            if not await db.attendance.find_one({"student_id": s["id"], "date": d}):
                import random
                status = random.choices(["present", "absent", "half_day"], weights=[70, 20, 10])[0]
                await db.attendance.update_one(
                    {"student_id": s["id"], "date": d, "subject_id": subject_ids[0] if subject_ids else ""},
                    {"$set": {"id": str(uuid.uuid4()), "student_id": s["id"], "date": d, "status": status, "subject_id": subject_ids[0] if subject_ids else "", "marked_by": user["id"], "created_at": datetime.now(timezone.utc).isoformat()}},
                    upsert=True
                )

    settings = await db.settings.find_one({"id": "global"}, {"_id": 0})
    ranges = settings.get("marks_ranges", {}) if settings else {}
    for s in all_students:
        for sid in subject_ids[:2]:
            import random
            score = random.randint(30, 100)
            category = categorize_marks(score, 100, ranges)
            await db.marks.update_one(
                {"student_id": s["id"], "subject_id": sid, "exam_type": "midterm"},
                {"$set": {"id": str(uuid.uuid4()), "student_id": s["id"], "subject_id": sid, "score": score, "max_score": 100, "exam_type": "midterm", "category": category, "created_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )

    return {"message": f"Sample data seeded: {added_students} students, {len(subject_ids)} subjects, 7 days attendance, marks for all"}

# ===== OCR Integration =====
@api_router.post("/ocr")
async def ocr_endpoint(file: UploadFile = File(...), user=Depends(get_current_user)):
    if not file:
        logger.error("Frontend upload error: No file received in request.")
        raise HTTPException(status_code=400, detail="Frontend upload error: No file received")
    
    try:
        image_bytes = await file.read()
        if not image_bytes:
            raise ValueError("Empty image bytes")
    except Exception as e:
        logger.error(f"Backend file parsing error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Backend file parsing error: {str(e)}")

    try:
        text, numbers = detect_text(image_bytes)
        return {"text": text, "numbers": numbers}
    except Exception as e:
        logger.error(f"EasyOCR API error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"EasyOCR API error: {str(e)}")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware, allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"], allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory="static/static"), name="static")

@app.get("/")
def serve_react():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    p = Path("static") / full_path
    if p.is_file():
        return FileResponse(p)
    return FileResponse("static/index.html")

@app.on_event("startup")
async def seed_data():
    admin = await db.users.find_one({"role": "admin"})
    if not admin:
        await db.users.insert_one({
            "id": str(uuid.uuid4()), "email": "admin@abc.edu",
            "password_hash": pwd_context.hash("admin123"),
            "name": "Administrator", "role": "admin", "student_id": "",
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info("Admin seeded: admin@abc.edu / admin123")
    settings = await db.settings.find_one({"id": "global"})
    if not settings:
        await db.settings.insert_one({
            "id": "global", "college_name": DEFAULT_COLLEGE,
            "department_name": DEFAULT_DEPT, "logo_url": GGSP_LOGO,
            "splash_duration": 3, "attendance_alert_limit": 75,
            "theme": {"primary_color": "#4F5BD5", "secondary_color": "#0D9488", "dark_mode": False},
            "marks_ranges": {"L1": {"min": 75, "max": 100, "label": "Excellent"}, "L2": {"min": 50, "max": 74, "label": "Average"}, "L3": {"min": 0, "max": 49, "label": "Below Average"}}
        })
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.students.create_index("id", unique=True)
    await db.students.create_index("roll_no", unique=True)
    await db.subjects.create_index("id", unique=True)
    await db.attendance.create_index([("student_id", 1), ("date", 1), ("subject_id", 1)])
    await db.marks.create_index([("student_id", 1), ("subject_id", 1)])

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
