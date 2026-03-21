import requests
import pandas as pd
import time
import html
import json
import os
from bs4 import BeautifulSoup
from itertools import product
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== 설정값 =====
MAX_WORKERS = 5  # 동시 요청 수
TIMEOUT_SEC = 30 
MIN_EXPECTED_MAPS = 30
POST_RETRY_ROUNDS = 5

DEFAULT_MAPS = [
    "all-maps", "volskaya-industries", "temple-of-anubis", "hanamura",
    "throne-of-anubis", "hanaoka", "antarctic-peninsula", "nepal", "lijiang-tower",
    "busan", "samoa", "oasis", "ilios", "route-66", "watchpoint-gibraltar", "dorado",
    "rialto", "shambali-monastery", "circuit-royal", "junkertown", "havana", "new-junk-city",
    "suravasa", "aatlis", "numbani", "midtown", "blizzard-world", "eichenwalde",
    "kings-row", "paraiso", "hollywood", "new-queen-street", "runasapi", "esperanca", "colosseo"
]

# ===== 맵 목록 파싱 =====
def fetch_maps_from_web():
    """
    오버워치 사이트에서 맵 목록을 파싱하여 반환
    """
    try:
        print("🗺️  웹사이트에서 맵 목록 파싱 중...")
        url = "https://overwatch.blizzard.com/ko-kr/rates/"
        res = requests.get(url, timeout=TIMEOUT_SEC)
        res.raise_for_status()
        
        soup = BeautifulSoup(res.text, "html.parser")
        
        # 맵 선택 옵션 찾기
        map_select = soup.find("select", {"id": "filter-map-select"})
        if not map_select:
            map_select = soup.find("select", {"name": "filter-map-select"})

        if map_select:
            map_options = map_select.find_all("option", {"data-title": True})
        else:
            map_options = soup.find_all("option", {"data-title": True})
        
        maps = []
        for option in map_options:
            # value가 있고 data-title이 있는 맵 옵션만 추출
            map_value = option.get("value")
            map_title = option.get("data-title")
            
            if map_value and map_title:
                # 맵 선택 드롭다운의 옵션들만 (게임모드나 다른 드롭다운 제외)
                # parent select 태그 확인하여 맵 관련인지 체크
                parent = option.find_parent("select")
                if not parent or "map" in (parent.get("name", "") + parent.get("id", "")).lower():
                    maps.append({
                        "value": map_value,
                        "title": map_title
                    })
        
        # 간단한 방법: value 속성으로 맵 관련 옵션인지 판단
        # (맵은 보통 하이픈으로 연결된 문자열)
        if not maps:
            # parent 체크가 안되면 value 패턴으로 필터링
            maps = [
                {"value": opt.get("value"), "title": opt.get("data-title")}
                for opt in map_options
                if opt.get("value") and "-" in opt.get("value", "")
            ]
        
        map_values = [m["value"] for m in maps]
        print(f"✅ {len(map_values)}개 맵 파싱 완료")
        
        # 맵 정보 저장
        maps_data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "maps": maps,
            "map_values": map_values
        }
        
        maps_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps.json")
        with open(maps_file, "w", encoding="utf-8") as f:
            json.dump(maps_data, f, ensure_ascii=False, indent=2)
        print(f"💾 맵 정보 저장: {maps_file}")
        
        return map_values
        
    except Exception as e:
        print(f"⚠️ 맵 파싱 실패: {e}")
        print("기본 맵 목록 사용")
        return None

def load_maps():
    """
    저장된 맵 목록을 로드하거나, 없으면 웹에서 파싱
    """
    maps_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps.json")
    
    # 저장된 맵 파일이 있는지 확인
    if os.path.exists(maps_file):
        try:
            with open(maps_file, "r", encoding="utf-8") as f:
                maps_data = json.load(f)
            map_values = maps_data.get("map_values", [])
            print(f"📂 저장된 맵 목록 로드: {len(map_values)}개")
            if len(map_values) >= MIN_EXPECTED_MAPS:
                return map_values
            print("⚠️ 저장된 맵 목록이 충분하지 않아 재파싱 시도")
        except Exception as e:
            print(f"⚠️ 맵 파일 로드 실패: {e}")
    
    # 저장된 파일이 없거나 로드 실패 시 웹에서 파싱
    maps = fetch_maps_from_web()
    
    if maps and len(maps) >= MIN_EXPECTED_MAPS:
        return maps
    if maps:
        missing = [m for m in DEFAULT_MAPS if m not in maps]
        if missing:
            maps = maps + missing
        return maps
    
    # 파싱도 실패한 경우 기본 맵 목록 반환
    print("⚠️ 기본 하드코딩된 맵 목록 사용")
    return DEFAULT_MAPS

def scrape_single_url(args):
    region, input_gamemode, map_name, tier, date_str = args
    
    # [수정됨] 폴백 로직 설정
    # 요청 들어온 모드가 2(경쟁전)이면 [2, 1] 순서로 시도
    # 그 외(빠른대전 0 등)는 원래 값만 시도
    modes_to_try = [2, 1] if input_gamemode == 2 else [input_gamemode]

    # 설정된 모드 후보들을 순차적으로 시도
    for current_gamemode in modes_to_try:
        records = []
        
        # URL 생성
        base_url = "https://overwatch.blizzard.com/ko-kr/rates/"
        # rq 파라미터에 현재 시도 중인 current_gamemode 사용
        params = f"?input=pc&map={map_name}&region={region}&role=All&rq={current_gamemode}&tier={tier}"
        target_url = base_url + params

        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = requests.get(target_url, timeout=TIMEOUT_SEC)
                res.raise_for_status()

                soup = BeautifulSoup(res.text, "html.parser")

                # ================================================================
                # 🛡️ HTML 태그(Select Option) 3중 검증
                # ================================================================

                # [1] 게임 모드 검증 (현재 시도 중인 모드와 일치하는지 확인)
                selected_gamemode = soup.find("option", {"value": str(current_gamemode), "selected": True})
                if not selected_gamemode:
                    # 검증 실패 시, 다음 시도(retries)가 아니라 다음 모드(modes_to_try)로 넘어가야 하므로
                    # 여기서는 빈 리스트 반환하고 loop break 유도
                    break 

                # [2] 맵 검증
                if map_name != "all-maps":
                    selected_map = soup.find("option", {"value": map_name, "selected": True})
                    if not selected_map:
                        break

                # [3] 티어 검증
                if tier != "All":
                    selected_tier = soup.find("option", {"value": tier, "selected": True})
                    if not selected_tier:
                        break
                
                # ================================================================

                # 데이터 추출
                tag = soup.find("blz-data-table")
                if not tag:
                    break

                raw_json = html.unescape(tag["allrows"])
                data = json.loads(raw_json)

                if not data: 
                    break

                for hero in data:
                    cells = hero.get("cells", {})
                    hero_meta = hero.get("hero", {})
                    records.append({
                        "date": date_str,
                        # [수정됨] 1, 2 모두 "competitive"로 기록, 0은 "quickplay"
                        "game_mode": "competitive" if current_gamemode in [1, 2] else "quickplay",
                        "region": region,
                        "map": map_name,
                        "tier": tier,
                        "hero": cells.get("name", ""),
                        "role": hero_meta.get("role", ""),
                        "pick_rate": cells.get("pickrate", ""),
                        "win_rate": cells.get("winrate", "")
                    })
                
                time.sleep(0.1)
                
                # 데이터를 성공적으로 찾았으면 즉시 반환 (더 이상 다른 모드/재시도 불필요)
                return records

            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    pass # 마지막 시도 실패 시 다음 로직으로 이동

        # 만약 records가 채워졌다면 루프 종료 및 반환 (위의 return records가 처리함)
        # 여기까지 왔다는 건, 현재 current_gamemode로는 실패했다는 뜻
        # 다음 modes_to_try로 넘어감 (예: 2 실패 -> 1 시도)

    return []

def task_to_mode_str(task):
    _, gamemode, _, _, _ = task
    return "competitive" if gamemode == 2 else "quickplay"

def build_expected_heroes_by_mode(task_results):
    expected = {"quickplay": set(), "competitive": set()}

    # 우선순위 1: all-maps + All 조합에서 영웅 풀 추출
    for task, records in task_results.items():
        _, _, map_name, tier, _ = task
        if map_name != "all-maps" or tier != "All":
            continue

        mode_str = task_to_mode_str(task)
        heroes = {r.get("hero", "") for r in records if r.get("hero")}
        expected[mode_str].update(heroes)

    # 우선순위 2: 위 값이 비어있으면 해당 모드 전체에서 최대한 수집
    for task, records in task_results.items():
        mode_str = task_to_mode_str(task)
        if expected[mode_str]:
            continue
        heroes = {r.get("hero", "") for r in records if r.get("hero")}
        expected[mode_str].update(heroes)

    return expected

def find_retry_tasks(task_results, expected_heroes_by_mode):
    retry_tasks = []

    for task, records in task_results.items():
        expected = expected_heroes_by_mode.get(task_to_mode_str(task), set())

        # 기대 영웅셋이 아직 없으면 판별 불가 -> 일단 스킵
        if not expected:
            continue

        actual = {r.get("hero", "") for r in records if r.get("hero")}
        if len(actual) < len(expected):
            retry_tasks.append(task)

    return retry_tasks

def main():
    # ===== 0. 기본 설정 =====
    date_str = datetime.now().strftime("%Y-%m-%d")
    season_dir = "Season20" # 필요시 수정
    season_num = "".join(ch for ch in season_dir if ch.isdigit())
    season_code = f"S{season_num}"
    date_short = datetime.strptime(date_str, "%Y-%m-%d").strftime("%y%m%d")

    save_root = os.path.join(season_dir, date_str)
    os.makedirs(save_root, exist_ok=True)

    print(f"=== Saving data under: {save_root} ===")
    print(f"=== Workers: {MAX_WORKERS} threads ===")

    # ===== 1. 수집 대상 설정 (순서 정의) =====
    # 이 리스트 순서대로 최종 파일이 정렬됩니다.
    gamemodes = [0, 2] # 0:quickplay, 2:competitive (실패시 1로 자동 폴백)
    regions = ["Americas", "Europe", "Asia"]
    
    # 웹사이트에서 맵 목록 로드 (또는 저장된 파일에서)
    maps = load_maps()
    
    tiers = ["All", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master", "Grandmaster"]

    total_rows = 0

    # 정렬을 위한 텍스트 변환 맵핑
    # [수정됨] 2가 들어와도 결과값은 competitive이므로 2->competitive 매핑 유지
    mode_map_str = {0: "quickplay", 2: "competitive"} 
    ordered_modes = [mode_map_str[g] for g in gamemodes] 

    # ===== 2. 지역별 수집 =====
    for region in regions:
        print(f"\n===== 🌎 {region} 수집 시작 (Parallel) =====")
        
        tasks = []
        for gamemode, map_name, tier in product(gamemodes, maps, tiers):
            # [조건부 건너뛰기]
            # 빠대(0)인데 티어가 전체가 아니면 스킵
            if gamemode == 0 and tier != "All": continue
            
            # 경쟁전(1,2)에서 특정 맵 스킵 (사용자 원본 로직 유지)
            # 여기서는 2로 진입하므로 2일 때 검사
            if gamemode == 2 and map_name in ["volskaya-industries", "temple-of-anubis", "hanamura", "throne-of-anubis", "hanaoka", "antarctic-peninsula"]: continue
            
            tasks.append((region, gamemode, map_name, tier, date_str))

        task_results = {t: [] for t in tasks}
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_url = {executor.submit(scrape_single_url, t): t for t in tasks}
            
            for i, future in enumerate(as_completed(future_to_url)):
                try:
                    data = future.result()
                    task = future_to_url[future]
                    task_results[task] = data if data else []
                except Exception as exc:
                    print(f"Error: {exc}")
                
                if (i + 1) % 50 == 0:
                    print(f"    ... {i + 1}/{len(tasks)} 완료")

        # ===== 2-1. 누락 영웅 후속 재시도 =====
        expected_heroes_by_mode = build_expected_heroes_by_mode(task_results)
        retry_tasks = find_retry_tasks(task_results, expected_heroes_by_mode)

        if retry_tasks:
            print(f"🔁 {region} 누락 의심 조합 {len(retry_tasks)}건 후속 재시도 시작")

        for round_idx in range(1, POST_RETRY_ROUNDS + 1):
            if not retry_tasks:
                break

            print(f"    ↳ 재시도 라운드 {round_idx}: {len(retry_tasks)}건")
            improved_count = 0

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_task = {executor.submit(scrape_single_url, t): t for t in retry_tasks}

                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        new_data = future.result() or []
                    except Exception:
                        new_data = []

                    old_data = task_results.get(task, [])
                    old_heroes = {r.get("hero", "") for r in old_data if r.get("hero")}
                    new_heroes = {r.get("hero", "") for r in new_data if r.get("hero")}

                    if len(new_heroes) > len(old_heroes):
                        task_results[task] = new_data
                        improved_count += 1

            expected_heroes_by_mode = build_expected_heroes_by_mode(task_results)
            retry_tasks = find_retry_tasks(task_results, expected_heroes_by_mode)

            if improved_count == 0:
                print("    ↳ 추가 개선 없음, 재시도 종료")
                break

        region_records = []
        for records in task_results.values():
            if records:
                region_records.extend(records)

        # ===== 3. 저장 및 정렬 (Sorting) =====
        if region_records:
            df_region = pd.DataFrame(region_records)
            
            # ---------------------------------------------------------
            # 🧹 [정렬 로직]
            # ---------------------------------------------------------
            
            df_region['game_mode'] = pd.Categorical(
                df_region['game_mode'], categories=ordered_modes, ordered=True
            )
            df_region['map'] = pd.Categorical(
                df_region['map'], categories=maps, ordered=True
            )
            df_region['tier'] = pd.Categorical(
                df_region['tier'], categories=tiers, ordered=True
            )

            df_region = df_region.sort_values(by=['game_mode', 'map', 'tier'])
            
            # ---------------------------------------------------------

            total_rows += len(df_region)

            filename = f"{season_code}_{region}_{date_short}.csv"
            filepath = os.path.join(save_root, filename)
            df_region.to_csv(filepath, index=False, encoding="utf-8-sig")
            print(f"💾 {region} 저장 완료 (정렬됨): {len(df_region)} rows")
        else:
            print(f"⚠️ {region} 데이터 없음")

    print(f"\n🎉 전체 완료! 총 데이터 행 수: {total_rows}")

    if "GITHUB_ENV" in os.environ:
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"TOTAL_ROWS={total_rows}\n")

if __name__ == "__main__":
    main()
