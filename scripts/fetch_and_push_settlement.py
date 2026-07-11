# -*- coding: utf-8 -*-
"""
화천이티씨 - 정산내역 자동 수집 → 수금대장(Firestore) 자동 반영 스크립트

GitHub Actions에서 매일 자동 실행됩니다. (Secrets에서 키값을 읽어옵니다)
로컬에서 직접 실행하려면 아래 환경변수를 미리 설정해야 합니다:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET,
  COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID,
  FIREBASE_SERVICE_ACCOUNT (JSON 문자열 전체)
"""

import base64
import bcrypt
import hashlib
import hmac
import json
import os
import sys
import time
import datetime
import requests
import firebase_admin
from firebase_admin import credentials, firestore

NAVER_TOKEN_URL = "https://api.commerce.naver.com/external/v1/oauth2/token"
COUPANG_DOMAIN = "https://api-gateway.coupang.com"


def env(name):
    v = os.environ.get(name)
    if not v:
        print(f"환경변수 {name} 가 설정되지 않았습니다.")
        sys.exit(1)
    return v


# ---------- Firebase ----------

def init_firestore():
    sa_json = env("FIREBASE_SERVICE_ACCOUNT")
    cred = credentials.Certificate(json.loads(sa_json))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def get_year_doc(db, year):
    return db.collection("soogeum").document(str(year))


def load_existing_order_nos(doc_ref):
    snap = doc_ref.get()
    if not snap.exists:
        return set(), []
    data = snap.to_dict()
    misugeum = json.loads(data.get("misugeum", "[]"))
    existing = {str(it.get("naverOrderNo")) for it in misugeum if it.get("naverOrderNo")}
    return existing, misugeum


def push_new_entries(doc_ref, misugeum, new_entries):
    if not new_entries:
        return
    combined = new_entries + misugeum  # 최신 항목을 앞에 (unshift와 동일한 순서)
    doc_ref.set({
        "misugeum": json.dumps(combined, ensure_ascii=False),
        "updatedAt": datetime.datetime.utcnow().isoformat(),
    }, merge=True)


def update_settle_meta(db, platform, latest_date_str):
    """latest_date_str: YYYY-MM-DD"""
    if not latest_date_str:
        return
    ref = db.collection("soogeum").document("settleMeta")
    snap = ref.get()
    meta = {}
    if snap.exists:
        meta = json.loads(snap.to_dict().get("data", "{}"))
    y, m, d = latest_date_str.split("-")
    meta[platform] = f"{y}년 {int(m)}월 {int(d)}일까지"
    ref.set({"data": json.dumps(meta, ensure_ascii=False), "updatedAt": datetime.datetime.utcnow().isoformat()})


def make_id():
    return int(time.time() * 1000) + (os.getpid() % 1000)


# ---------- 네이버 ----------

def naver_get_token():
    client_id = env("NAVER_CLIENT_ID")
    client_secret = env("NAVER_CLIENT_SECRET")
    timestamp = str(int(time.time() * 1000))
    password = f"{client_id}_{timestamp}"
    hashed = bcrypt.hashpw(password.encode("utf-8"), client_secret.encode("utf-8"))
    sign = base64.b64encode(hashed).decode("utf-8")
    data = {
        "client_id": client_id, "timestamp": timestamp,
        "client_secret_sign": sign, "grant_type": "client_credentials", "type": "SELF",
    }
    res = requests.post(NAVER_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    result = res.json()
    if "access_token" not in result:
        print("[네이버] 토큰 발급 실패:", json.dumps(result, ensure_ascii=False))
        return None
    return result["access_token"]


def fetch_naver_entries(existing_order_nos):
    """
    실험적: 정확한 정산 API 경로가 아직 확정되지 않아 후보 경로들을 시도합니다.
    성공하면 [수금대장 misugeum 항목, ...] 리스트와 최신 정산완료일을 반환합니다.
    실패하면 (빈 리스트, None) 을 반환하고 이유를 로그로 남깁니다.
    """
    token = naver_get_token()
    if not token:
        return [], None

    candidate_paths = [
        "/external/v1/seller/settle-summaries",
        "/external/v1/settle/summary",
        "/external/v1/settlements",
        "/external/v1/seller/settlements",
    ]
    headers = {"Authorization": f"Bearer {token}"}
    today = datetime.date.today()
    from_date = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    for path in candidate_paths:
        url = f"https://api.commerce.naver.com{path}"
        try:
            res = requests.get(url, headers=headers, params={"fromDate": from_date, "toDate": to_date})
        except requests.RequestException as e:
            print(f"[네이버] {path} 호출 오류: {e}")
            continue
        print(f"[네이버] 시도: {path} -> {res.status_code}")
        if res.status_code == 200:
            try:
                rows = res.json()
            except ValueError:
                continue
            # TODO: 정확한 응답 스키마 확인 후 misugeum 항목으로 변환하는 로직 완성 필요
            print("[네이버] 응답 성공! 아래 구조를 확인해서 변환 로직을 완성해야 합니다:")
            print(json.dumps(rows, ensure_ascii=False, indent=2)[:1500])
            return [], None
        else:
            print("   ", res.text[:200])

    print("[네이버] 모든 후보 경로 실패. 정확한 API 경로 확인이 더 필요합니다.")
    return [], None


# ---------- 쿠팡 ----------

def coupang_auth_header(method, path, query, access_key, secret_key):
    os.environ["TZ"] = "GMT+0"
    try:
        time.tzset()
    except AttributeError:
        pass
    datetime_str = time.strftime("%y%m%d") + "T" + time.strftime("%H%M%S") + "Z"
    message = datetime_str + method + path + (query or "")
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={datetime_str}, signature={signature}"


def coupang_call(method, path, query=""):
    access_key = env("COUPANG_ACCESS_KEY")
    secret_key = env("COUPANG_SECRET_KEY")
    url = COUPANG_DOMAIN + path + (("?" + query) if query else "")
    headers = {
        "Authorization": coupang_auth_header(method, path, query, access_key, secret_key),
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-By": env("COUPANG_VENDOR_ID"),
    }
    return requests.request(method, url, headers=headers)


def fetch_coupang_entries(existing_order_nos):
    vendor_id = env("COUPANG_VENDOR_ID")
    today = datetime.date.today()
    date_from = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    order_map = {}
    token = ""
    while True:
        path = "/v2/providers/openapi/apis/api/v1/revenue-history"
        query = f"vendorId={vendor_id}&recognitionDateFrom={date_from}&recognitionDateTo={date_to}&token={token}&maxPerPage=50"
        res = coupang_call("GET", path, query)
        try:
            data = res.json()
        except ValueError:
            print("[쿠팡] 응답 파싱 실패:", res.status_code, res.text[:300])
            break
        if data.get("code") not in (200, "200"):
            print("[쿠팡] 오류:", json.dumps(data, ensure_ascii=False)[:500])
            break
        for order in data.get("data", []):
            order_no = str(order.get("orderId"))
            settle_date = order.get("settlementDate", "")
            for item in order.get("items", []):
                sale = item.get("salePrice", 0)
                fee = abs(item.get("serviceFee", 0))
                settle_amt = sale - fee
                if order_no not in order_map:
                    order_map[order_no] = {"amt": 0, "date": settle_date, "product": "", "name": ""}
                order_map[order_no]["amt"] += settle_amt
                if not order_map[order_no]["product"]:
                    order_map[order_no]["product"] = item.get("productName", "")
        if not data.get("hasNext"):
            break
        token = data.get("nextToken", "")
        if not token:
            break

    new_entries = []
    latest_date = None
    for order_no, v in order_map.items():
        if v["amt"] <= 0:
            continue
        if order_no in existing_order_nos:
            continue
        if not v["date"]:
            continue
        if latest_date is None or v["date"] > latest_date:
            latest_date = v["date"]
        y, m, d = v["date"].split("-")
        date_str = f"{int(m)}/{int(d)}"
        product = v["product"]
        short_product = (product[:25] + "…") if len(product) > 25 else product
        new_entries.append({
            "desc": f"[쿠팡]  {short_product}",
            "company": "[쿠팡] ",
            "content": short_product,
            "amt": round(v["amt"]),
            "date": date_str,
            "done": True,
            "tax": "issued",
            "isSplit": False,
            "splits": [],
            "naverOrderNo": order_no,
            "id": make_id(),
        })
    return new_entries, latest_date


def main():
    db = init_firestore()
    year = datetime.date.today().year
    doc_ref = get_year_doc(db, year)

    existing_order_nos, misugeum = load_existing_order_nos(doc_ref)
    print(f"기존 등록된 주문번호 수: {len(existing_order_nos)}")

    print("\n===== 쿠팡 정산 수집 =====")
    coupang_entries, coupang_latest = fetch_coupang_entries(existing_order_nos)
    print(f"[쿠팡] 새로 추가할 항목: {len(coupang_entries)}건")

    print("\n===== 네이버 정산 수집 (실험적) =====")
    naver_entries, naver_latest = fetch_naver_entries(existing_order_nos)
    print(f"[네이버] 새로 추가할 항목: {len(naver_entries)}건")

    all_new = coupang_entries + naver_entries
    if all_new:
        push_new_entries(doc_ref, misugeum, all_new)
        print(f"\nFirestore에 {len(all_new)}건 반영 완료!")
    else:
        print("\n새로 추가할 항목이 없습니다.")

    if coupang_latest:
        update_settle_meta(db, "coupang", coupang_latest)
    if naver_latest:
        update_settle_meta(db, "naver", naver_latest)


if __name__ == "__main__":
    main()
