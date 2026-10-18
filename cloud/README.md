# meta-search Lambda

로컬 MCP에서 분리한 임베딩·전체 재군집화 실행 환경이다. 클라우드 전용 의존성·모델·Docker 배포 코드는 MCP wheel/sdist와 Antigravity2 복사 대상에서 제외한다.

## 요청·저장·응답

`POST Function URL`에 `Authorization: Bearer TOKEN`을 넣는다.

```json
{"units":[{"unit_id":1,"review_id":1,"category_id":"keyboard","raw_aspect":"키감","raw_status":"좋음"}]}
```

`unit_id`, `review_id`는 전송하는 클라이언트 DB의 정수 ID다. 코드 whitelist의 토큰 해시별 사용자 구분값으로 서로 다른 클라이언트의 ID를 구분한다. 같은 입력 재전송은 기존 입력을 덮어쓴다. 리뷰 ID는 대표 표현의 리뷰 수 계산에 사용한다. 리뷰 본문·excerpt·감성은 받지 않는다.

Lambda는 S3의 **latest.sqlite 하나**를 내려받고 신규 Units를 합친 뒤, 누적 입력 전체를 카테고리별로 다시 군집화한다. 기존 KR-SBERT·complete-linkage·반대 상태 보호·대표 표현 규칙을 유지한다. SQLite에는 입력 Units, 속성·상태 군집, Exact-match 표만 저장한다. 입력은 누적하며 로컬 리뷰 교체에 따른 과거 입력 삭제 동기화는 수행하지 않는다.

최신 DB를 같은 S3 키에 덮어쓰고 다음 JSON을 반환한다.

```json
{
  "aspects": [{"category_id":"keyboard","raw_aspect":"키감","aspect_cluster_id":"ac_...","label":"타건감"}],
  "states": [{"category_id":"keyboard","aspect_cluster_id":"ac_...","raw_status":"좋음","state_cluster_id":"sc_...","label":"좋음"}]
}
```

`raw_aspect`/`raw_status` 키는 기존 `norm_text`로 정리된 문자열이다. 속성 표현을 먼저 Exact-match로 매핑하고, 상태는 해당 속성 군집 ID와 상태 표현으로 조회한다. 표는 전 카테고리의 최신 결과이며, 로컬은 자기 DB에 있는 카테고리만 적용한다. 군집 버전·과거 DB·증분 군집화·동시 접근 제어는 없다.

## 배포

AWS CLI 로그인과 실행 중인 Docker, `cloud/models/`의 기존 고정 리비전 KR-SBERT 파일이 필요하다. 최초 모델 준비가 필요하면 `config.json`의 model_id/revision으로 Hugging Face 파일을 이 경로에 내려받는다. Lambda 호출 중 모델 다운로드는 수행하지 않는다.

```bash
uv run --with 'boto3[crt]' python cloud/deploy.py --region ap-northeast-2
```

전용 S3/ECR 저장소 스택 `meta-search-clustering-storage`와 함수 스택 `meta-search-clustering`를 생성·갱신한다. Lambda는 Python 3.12의 x86_64 이미지, 3008MB 메모리, 900초 제한을 사용한다. 실행 역할은 S3 `latest.sqlite` 읽기·쓰기와 CloudWatch 로그에만 접근한다. S3 버전 관리를 켜지 않는다.

최초 배포 시 로컬 운영 DB의 기존 입력·Exact-match 표로 S3를 초기화한다. 기존 S3 DB가 있으면 재배포로 덮어쓰지 않는다. 함수 URL은 로컬 `config.toml`에 기록하며, 배포 결과는 `cloud/.build/deployment.json`에 저장한다.

## 토큰 whitelist

`allowed_tokens.json`의 항목은 `SHA-256 토큰 해시: 사용자 구분값`이다. 최초 배포는 본인 토큰 한 개를 등록하고 이후에는 기존 식별값을 유지한다. 파일이 없으면 새로 생성한다. whitelist는 Git에서 제외한다. 토큰 평문은 로컬 `data/cluster-token`에 0600 권한으로 저장하며 모델 이미지에는 포함하지 않는다.

친구를 추가하려면 개인별로 토큰을 발급하고 해당 토큰의 SHA-256 해시와 고유 구분값을 `allowed_tokens.json`에 추가한 후 재배포한다. 각자는 `META_SEARCH_CLUSTER_TOKEN` 환경 변수 또는 자신의 `data/cluster-token` 파일을 사용한다. 동일 토큰은 하나의 클라이언트 DB에서만 사용한다. 토큰 없는 요청은 S3·모델 접근 전에 403으로 거절한다. 회원 가입·IP 정책·클라이언트 AWS SDK 설정은 없다.

본인의 기존 식별값을 바꾸려면 다음처럼 재배포한다.

```bash
uv run --with 'boto3[crt]' python cloud/deploy.py --owner-alias '새로운-고유식별값'
```

토큰은 유지하고, S3 최신 DB의 기존 본인 입력과 리뷰 ID의 사용자 구분 접두사도 새 식별값으로 옮긴다. 같은 식별값으로 다시 배포하면 이관하지 않는다. Value는 표시용 닉네임이 아니라 입력 저장 키이므로 이미 사용하는 값은 임의로 수정하지 않는다. 현재 본인의 값은 `jaewonE:7701`이다.

## 검증

로컬 MCP 회귀 검증은 프로젝트 루트에서 `uv run pytest -q`로 수행한다. Lambda 전용 군집화 테스트는 NumPy·scikit-learn이 있는 Lambda 컨테이너에서 `PYTHONPATH=/var/task pytest /tests`로 별도 수행한다. 실 AWS 호출·S3 저장 검증 결과는 루트 `VERIFICATION.md`에 기록한다.
