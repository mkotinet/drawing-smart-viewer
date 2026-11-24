# 도면 스마트 뷰어 (Drawing Smart Viewer)

P&ID · 계장 결선도 · 배선도 · 장비 배치도 · 시공도(SHOP)를 **로컬 PC에서 1회 인덱싱·WebP 타일링**한 뒤, **Synology NAS(DS920+)의 초경량 FastAPI PWA 컨테이너(`RAM 256MB 캡`)**로 동기화하여 모바일·PC에서 0.2초 만에 열람·교차 검색하는 로컬 전용 도면 뷰어입니다.

## 핵심 특징
- **초경량 NAS 운영**: NAS 컨테이너에 ODA/OpenCV/OCR/pypdfium2 미탑재 (`python:3.12-slim` + `FastAPI` + `SQLite FTS5`, 상시 RAM ~60MB, `mem_limit: 256m`, `cpu_shares: 512`).
- **2단계 지연 타일 피라미드**: `preview.webp` 즉시 표시 후 1.25배 이상 확대 시 화면 영역의 1024px WebP 타일만 지연 로드 + 1년 `immutable` 캐시.
- **계기 태그 교차 탐색**: PDF/CAD에서 추출한 태그 좌표(`x0,y0,x1,y1`)로 자동 줌/하이라이트 및 P&ID ↔ 결선도 ↔ 배선도 ↔ 데이터시트 원클릭 교차 이동.
- **PWA & 보안**: 무캐시 `manifest.json`, Network-First + Cache-First 오프라인 타일 `sw.js`, PBKDF2 인증, 모바일 영구 세션(401 명시 수신 시에만 만료), KST 감사 로그.

## 로컬 PC 인덱싱 & 동기화
```bash
uv run --python 3.12 --no-project --with pypdfium2 --with pillow --with pypdf \
  python scripts/build_index.py --input raw_drawings --out data

# 서버로 18MB 청크 단위 동기화
python scripts/build_index.py --sync-only --out data --url https://<도메인> --token <SYNC_TOKEN>
```
