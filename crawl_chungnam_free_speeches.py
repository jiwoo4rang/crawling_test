import csv
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "https://council.chungnam.go.kr/kr/minutes/free.do"

OUT_DIR = Path("output_chungnam_free")
OUT_DIR.mkdir(exist_ok=True)

POST_CSV_DIR = OUT_DIR / "posts_csv"
POST_CSV_DIR.mkdir(exist_ok=True)

HEADLESS = True
TIMEOUT = 40000
TEST_LIMIT = None

RETRY_COUNT = 2
RETRY_WAIT_MS = 2500


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_paragraphs(text: str) -> str:
    text = clean_text(text)

    remove_patterns = [
        r"글자크기조절,?\s*인쇄",
        r"SNS 공유",
        r"URL 복사",
        r"홈페이지",
        r"5분발언",
        r"제목, 대수, 회기, 차수, 의원, 날짜 내용 확인할 수 있습니다",
        r"회의록 보기",
        r"영상 회의록 보기",
        r"\b보기\b",
        r"\b등록\b",
        r"\b매우만족\b",
        r"\b만족\b",
        r"\b보통\b",
        r"\b불만족\b",
        r"\b매우불만족\b",
    ]

    for pattern in remove_patterns:
        text = re.sub(pattern, "", text)

    lines = [line.strip() for line in text.split("\n")]
    cleaned_lines = []

    for line in lines:
        if not line:
            cleaned_lines.append("")
            continue
        if line in {"목록"}:
            continue
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    paragraphs = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue

        para_lines = [x.strip() for x in para.split("\n") if x.strip()]
        joined = " ".join(para_lines)
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        if joined:
            paragraphs.append(joined)

    return "\n\n".join(paragraphs).strip()


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] if name else "제목없음"


def get_existing_numbers(out_dir: Path) -> set[str]:
    existing = set()
    for file_path in out_dir.glob("*.csv"):
        m = re.match(r"^(\d{1,4})_", file_path.name)
        if m:
            existing.add(m.group(1))
    return existing


def save_single_csv(data, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)

    title = data.get("제목", "") or "제목없음"
    number = str(data.get("게시물번호", "")).strip()

    if number:
        filename = f"{number}_{safe_filename(title)}.csv"
    else:
        filename = f"{safe_filename(title)}.csv"

    file_path = out_dir / filename

    fieldnames = ["제목", "차수", "회의일", "의원", "내용"]

    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "제목": data.get("제목", ""),
            "차수": data.get("차수", ""),
            "회의일": data.get("회의일", ""),
            "의원": data.get("의원", ""),
            "내용": data.get("내용", ""),
        })

    print(f"[저장 완료] {file_path}")


def is_error_page(text: str) -> bool:
    text = clean_text(text).lower()
    error_signals = [
        "502 bad gateway",
        "504 gateway timeout",
        "500 internal server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "temporarily unavailable",
        "nginx error",
    ]
    return any(signal in text for signal in error_signals)


def get_total_count(page):
    body = clean_text(page.locator("body").inner_text())
    m = re.search(r"게시물\s*:\s*\d+\s*~\s*\d+\s*/\s*(\d+)", body)
    if m:
        return int(m.group(1))
    return 355


def goto_page(page, page_no: int):
    url = f"{BASE_URL}?page={page_no}"
    page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
    page.wait_for_timeout(1500)


def extract_list_number_from_row_text(row_text: str) -> str:
    row_text = clean_text(row_text)

    m = re.match(r"^\s*(\d{1,4})\b", row_text)
    if m:
        return m.group(1)

    lines = [x.strip() for x in row_text.split("\n") if x.strip()]
    for line in lines[:3]:
        if re.fullmatch(r"\d{1,4}", line):
            return line

    return ""


def get_post_links(page):
    links = []
    seen = set()

    all_links = page.locator("a")
    count = all_links.count()

    for i in range(count):
        try:
            a = all_links.nth(i)
            text = clean_text(a.inner_text(timeout=1000))
            href = a.get_attribute("href")

            if not text or not href:
                continue

            if "freeview.do" not in href:
                continue

            if len(text) < 5:
                continue

            number = ""

            try:
                tr = a.locator("xpath=ancestor::tr[1]")
                if tr.count() > 0:
                    row_text = clean_text(tr.first.inner_text(timeout=1000))
                    number = extract_list_number_from_row_text(row_text)
            except Exception:
                pass

            if not number:
                try:
                    parent_text = clean_text(a.locator("xpath=..").inner_text(timeout=1000))
                    number = extract_list_number_from_row_text(parent_text)
                except Exception:
                    pass

            key = (text, href)
            if key in seen:
                continue
            seen.add(key)

            links.append({
                "number": number,
                "text": text,
                "href": href
            })

        except Exception:
            continue

    return links


def extract_real_title(body_text: str) -> str:
    body_text = clean_text(body_text)

    m = re.search(
        r"제목\s+[‘'\"「]?(.*?)[”'\"」]?\s*대수\s*제?\d+대",
        body_text,
        re.S
    )
    if m:
        title = clean_text(m.group(1))
        if title and title != "5분발언":
            return title

    lines = [x.strip() for x in body_text.split("\n") if x.strip()]

    for i, line in enumerate(lines):
        if line == "제목" and i + 1 < len(lines):
            candidate = clean_text(lines[i + 1])
            if candidate not in {"5분발언", "홈페이지", "목록"}:
                return candidate

    for line in lines:
        m2 = re.match(r"제목\s+(.+)", line)
        if m2:
            candidate = clean_text(m2.group(1))
            if candidate and "대수" not in candidate and candidate != "5분발언":
                return candidate

    for line in lines:
        if line in {"5분발언", "홈페이지", "목록"}:
            continue
        if "대수" in line or "차수" in line or "회의일" in line or "의원" in line:
            continue
        if len(line) >= 8:
            return clean_text(line)

    return ""


def extract_member_name(body_text: str) -> str:
    member = ""

    m_member1 = re.search(r"([가-힣]{2,4})\s*의원\s*내용", body_text)
    if m_member1:
        member = m_member1.group(1)

    if not member:
        candidates = re.findall(r"([가-힣]{2,4})\s*의원", body_text)
        blacklist = {
            "충남", "대전", "의회", "남도", "본", "우리",
            "존경", "선배", "동료", "국민", "사랑", "여러분"
        }

        for c in candidates:
            if c not in blacklist:
                member = c
                break

    return member


def extract_detail_data(page):
    data = {
        "게시물번호": "",
        "제목": "",
        "대수": "",
        "차수": "",
        "회의일": "",
        "의원": "",
        "내용": "",
    }

    try:
        body_text = clean_text(page.locator("body").inner_text())
    except Exception:
        body_text = ""

    data["제목"] = extract_real_title(body_text)

    m1 = re.search(r"대수\s*제?(\d+)대", body_text)
    if m1:
        data["대수"] = f"{m1.group(1)}대"

    m2 = re.search(r"회기\s*제?(\d+)회", body_text)
    if m2:
        data["차수"] = f"{m2.group(1)}회"

    m3 = re.search(r"회의일\s*([0-9]{4}[.\-/][0-9]{2}[.\-/][0-9]{2})", body_text)
    if m3:
        data["회의일"] = m3.group(1).replace(".", "-").replace("/", "-")

    data["의원"] = extract_member_name(body_text)

    full = body_text

    start_idx = -1
    if data["의원"]:
        pattern = rf"{re.escape(data['의원'])}\s*의원\s*내용"
        m_start = re.search(pattern, full)
        if m_start:
            start_idx = m_start.end()

    if start_idx == -1:
        m_start2 = re.search(r"의원\s*내용", full)
        if m_start2:
            start_idx = m_start2.end()

    if start_idx == -1:
        for s in ["사랑하는", "존경하는", "안녕하십니까?"]:
            idx = full.find(s)
            if idx != -1:
                start_idx = idx
                break

    end_idx = -1
    end_candidates = [
        "만족도 조사",
        "자료관리부서",
        "이 페이지에서 제공하는 정보에 대하여 만족하십니까?",
    ]

    for e in end_candidates:
        idx = full.find(e)
        if idx != -1:
            end_idx = idx
            break

    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        content = full[start_idx:end_idx]
    elif start_idx != -1:
        content = full[start_idx:]
    else:
        content = full

    remove_patterns = [
        r"글자크기조절,?\s*인쇄",
        r"SNS 공유",
        r"URL 복사",
        r"홈페이지",
        r"5분발언",
        r"제목, 대수, 회기, 차수, 의원, 날짜 내용 확인할 수 있습니다",
        r"회의록",
        r"회의록 보기",
        r"영상 회의록",
        r"영상 회의록 보기",
        r"보기",
        r"등록",
        r"매우만족",
        r"만족",
        r"보통",
        r"불만족",
        r"매우불만족",
        r"이 페이지에서 제공하는 정보에 대하여 만족하십니까\?",
        r"자료관리부서.*",
        r"만족도 조사.*",
    ]

    for pattern in remove_patterns:
        content = re.sub(pattern, "", content, flags=re.S)

    if data["의원"]:
        content = re.sub(rf"^{re.escape(data['의원'])}\s*의원\s*내용", "", content).strip()

    data["내용"] = normalize_paragraphs(content)

    return data


def build_absolute_url(href: str):
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://council.chungnam.go.kr{href}"
    return f"https://council.chungnam.go.kr/kr/minutes/{href}"


def fetch_detail_with_retry(browser, detail_url: str, post_text: str):
    for attempt in range(1, RETRY_COUNT + 1):
        detail_page = None
        try:
            detail_page = browser.new_page()
            detail_page.set_default_timeout(TIMEOUT)
            detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=TIMEOUT)
            detail_page.wait_for_timeout(1500)

            body_text = clean_text(detail_page.locator("body").inner_text())

            if is_error_page(body_text):
                print(f"[재시도 {attempt}/{RETRY_COUNT}] 에러 페이지 감지: {post_text}")
                detail_page.close()
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_WAIT_MS / 1000)
                    continue
                return None

            data = extract_detail_data(detail_page)
            detail_page.close()
            return data

        except Exception as e:
            print(f"[재시도 {attempt}/{RETRY_COUNT}] 상세 진입 실패: {post_text} / {e}")
            try:
                if detail_page:
                    detail_page.close()
            except Exception:
                pass

            if attempt < RETRY_COUNT:
                time.sleep(RETRY_WAIT_MS / 1000)
                continue

            return None


def main():
    collected = 0
    initial_existing_numbers = get_existing_numbers(POST_CSV_DIR)  # 실행 전 기존 파일
    seen_numbers_this_run = set()  # 이번 실행에서 새로 처리한 번호

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=HEADLESS,
            slow_mo=150
        )

        page = browser.new_page()
        page.set_default_timeout(TIMEOUT)

        goto_page(page, 1)
        total_count = get_total_count(page)
        total_pages = (total_count + 9) // 10

        print(f"[INFO] 총 게시물 수: {total_count}")
        print(f"[INFO] 총 페이지 수: {total_pages}")
        print(f"[INFO] 이미 저장된 게시물 수: {len(initial_existing_numbers)}")
        print("[INFO] 마지막 페이지부터 역순 수집 시작")

        for page_no in range(total_pages, 0, -1):
            print(f"\n[INFO] {page_no} 페이지 수집 시작")

            try:
                goto_page(page, page_no)
            except Exception as e:
                print(f"[건너뜀] 목록 페이지 이동 실패: {page_no} / {e}")
                continue

            posts = get_post_links(page)
            posts = list(reversed(posts))
            print(f"[INFO] 발견한 게시물 링크 수: {len(posts)}")

            for idx, post in enumerate(posts, start=1):
                if TEST_LIMIT is not None and collected >= TEST_LIMIT:
                    break

                number = str(post.get("number", "")).strip()

                # 실행 전에 이미 있던 파일 번호를 만나면 종료
                if number and number in initial_existing_numbers:
                    print(f"[중지] 기존 파일 발견: {number} / {post['text']}")
                    browser.close()
                    print("\n[완료]")
                    print(f"저장 폴더: {POST_CSV_DIR}")
                    return

                # 이번 실행에서 이미 처리한 번호면 중복 저장 방지
                if number and number in seen_numbers_this_run:
                    print(f"[건너뜀] 이번 실행에서 이미 처리한 번호: {number} / {post['text']}")
                    continue

                detail_url = build_absolute_url(post["href"])
                print(f"  - ({idx}/{len(posts)}) [{number}] {post['text']}")

                data = fetch_detail_with_retry(browser, detail_url, post["text"])
                if not data:
                    print(f"[실패] 최종 건너뜀: [{number}] {post['text']}")
                    continue

                data["게시물번호"] = number

                if not data["제목"]:
                    data["제목"] = post["text"]

                if is_error_page(data["제목"]) or is_error_page(data["내용"]):
                    print(f"[건너뜀] 저장 직전 에러 페이지 감지: [{number}] {post['text']}")
                    continue

                save_single_csv(data, POST_CSV_DIR)
                collected += 1

                if number:
                    seen_numbers_this_run.add(number)

                print(f"[진행중] 저장 완료: {number} / 이번 실행 저장 {collected}개")
                time.sleep(1)

            if TEST_LIMIT is not None and collected >= TEST_LIMIT:
                break

        browser.close()

    print("\n[완료]")
    print(f"저장 폴더: {POST_CSV_DIR}")


if __name__ == "__main__":
    main()