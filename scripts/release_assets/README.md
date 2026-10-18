# meta-search MCP 설치

상품 URL에서 객관 사양·리뷰 Opinion Units·판매 정보를 수집해 사용자별 로컬 카탈로그에 저장하는 stdio MCP입니다. 로컬은 Exact-match만 수행하고, 마지막 동기화 이후 새로 수집한 리뷰가 300건을 초과하면 지정된 Lambda에 임베딩·전체 재군집화를 요청합니다.

이 배포본에는 실행 코드, 프롬프트, JSON Schema, 설정, 의존성 잠금 파일, 설치 스크립트, 현재 빌드된 Exact-match 초기 표가 들어 있습니다. 기존 상품·리뷰·DB·로그·토큰·whitelist·AWS 관리 코드·모델·테스트·개발 문서는 포함하지 않습니다. 처음 설치하면 상품·리뷰가 없는 DB에 초기 매핑 표만 채워지고 자신의 데이터 폴더가 생성됩니다. 최초 설치에 Lambda 요청은 필요하지 않습니다.

초기 표 파일은 `bootstrap_exact_matches.json`입니다. 속성 표현 444개, 속성 군집 안의 상태 표현 518개와 카테고리 식별 정보만 포함하며 상품·리뷰 원문은 없습니다. 설치 직후 첫 리뷰부터 알려진 표현을 Exact-match로 매핑할 수 있습니다. 표에 없는 표현은 비매칭으로 남고 원래 표현 ID로 검색할 수 있습니다.

리뷰 300건까지는 재군집화를 요청하지 않고 301건부터 요청합니다. 리뷰 하나에서 여러 Units가 추출돼도 리뷰 한 건으로 셉니다. 매핑에 성공한 리뷰도 집계합니다. Lambda 요청에는 기존처럼 미전송 Units의 군집화 입력만 보내며, 성공 후 새 표로 전체 재매핑하고 리뷰 집계를 다시 시작합니다. 실패하면 미전송 표시와 수집 데이터를 유지합니다.

## 준비

- 설치 시 패키지를 내려받으므로 인터넷 연결이 필요합니다. Python 3.11 이상과 uv가 필요합니다. uv 설치는 [공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따릅니다.
- Codex CLI를 설치하고 본인 계정으로 로그인합니다. 상품 추출에 설정된 모델과 `--ignore-user-config`, `--output-schema`, `--ephemeral` 옵션을 지원하는 CLI가 필요합니다.
- agy CLI를 설치·로그인하고, agy에서 aside MCP 브라우저를 사용할 수 있도록 연결합니다. 페이지·리뷰 수집은 이 연결을 사용합니다. Codex에 이 MCP를 등록하는 것과 별도로 필요한 수집 환경입니다.
- 관리자에게 본인 전용 **토큰 원문**을 받습니다. AWS 계정·AWS CLI·Docker는 설치하지 않아도 됩니다.

## Codex 설치

ZIP을 압축 해제한 뒤 `meta-search` 폴더로 이동합니다. 설치한 폴더는 MCP가 계속 사용하므로 이후 임의로 옮기지 않습니다.

macOS/Linux:

```bash
python3 install.py
```

Windows PowerShell:

```powershell
py -3 install.py
```

설치 과정에서 토큰을 묻습니다. 입력은 화면에 표시되지 않으며 자신의 `data/cluster-token`에 저장됩니다. 설치 스크립트는 실행 파일 경로 확인, 가상환경 설치, 상품·리뷰가 없는 SQLite DB 생성 및 초기 매핑 표 적용, `codex mcp add` 등록을 수행합니다. 이미 사용한 데이터와 Lambda로 갱신한 매핑 표는 유지합니다. 설치를 마치면 Codex에서 meta-search MCP를 다시 연결합니다.

CLI가 PATH에 없으면 직접 지정합니다. 경로에 공백이 있으면 따옴표로 감쌉니다.

```bash
python3 install.py --codex-bin "/absolute/path/to/codex" --agy-bin "/absolute/path/to/agy"
```

토큰을 파일로 받은 경우 화면 입력 없이 설치할 수 있습니다.

```bash
python3 install.py --token-file "/absolute/path/to/my-token"
```

`--data-dir`로 자신의 데이터 저장 위치를 바꿀 수 있습니다. `--name`으로 Codex 등록 이름을 바꿀 수 있습니다. 기본 이름은 `meta-search`이며, 같은 이름이 이미 있으면 해당 등록을 이 설치 폴더로 갱신합니다.

## 다른 MCP 클라이언트

```bash
python3 install.py --no-register
```

마지막에 출력되는 `command`, `args`, `env`를 stdio MCP 설정에 사용합니다. 클라이언트가 달라도 상품 수집에는 Codex·agy CLI와 aside 연결이 필요합니다. 실행 시 작업 폴더가 달라도 `META_SEARCH_CONFIG`에 설치 폴더의 `config.toml` 절대 경로를 지정하면 됩니다.

## 설정·사용

- `config.toml`: 실행 파일·모델·한도·Lambda URL을 설정합니다. 기본 경로는 배포 폴더 기준 상대 경로입니다. 설치 시 CLI 경로는 현재 환경의 실제 경로로 기록합니다.
- 토큰 파일은 `paths.data_dir` 아래 `cluster-token`입니다. 환경 변수 `META_SEARCH_CLUSTER_TOKEN`을 사용하면 파일보다 우선합니다. 별칭은 서버가 토큰으로 구분하므로 사용자가 입력하지 않습니다.
- `get_search_guide`로 카테고리와 필터 키를 확인하고, `register_product`로 본인의 상품을 등록합니다. 초기 카탈로그가 비어 있는 것은 정상입니다.
- `get_registration_status`로 단계·경고·실패 원인을 확인합니다. macOS에서는 완료 알림도 요청합니다. 다른 운영체제에서는 작업 상태 조회를 사용합니다.
- 로그는 자신의 `data/jobs/`에 생성됩니다. 다른 사용자의 상품·리뷰를 로컬에 가져오지 않습니다. Lambda가 반환하는 표에는 같은 카테고리의 누적 정규화 표현이 포함될 수 있습니다.

설치 스크립트와 파일 잠금은 macOS/Linux/Windows를 고려해 작성했습니다. 실제 설치·stdio 검증은 macOS에서 수행했습니다. 다른 운영체제의 상품 수집 E2E는 검증하지 않았습니다.

## 라이선스

[GNU GPL 3.0](LICENSE)을 따릅니다.
