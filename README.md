# Agent Memory Template

杩欐槸涓€涓?Codex 闀挎湡璁板繂搴撴ā鏉裤€傚畠鎶婃櫘閫?Markdown 鏂囦欢褰撲綔闀挎湡浜嬪疄婧愶紝鐢?SQLite 寤哄叏搴撶储寮曪紝骞剁敤灏戦噺鍥哄畾瀛楁鏀寔鎸夌敤鎴枫€丄gent銆侀」鐩€佸簲鐢ㄣ€佷細璇濆拰璁板繂绫诲瀷杩囨护銆傞渶瑕佽涔夋绱㈡椂锛屼篃鍙互棰濆鍚敤鏈湴 EmbeddingGemma + Zvec 鍚戦噺鏃佽矾銆?

杩欎釜浠撳簱鍙寘鍚ā鏉裤€佽剼鏈拰鍋囩ず渚嬶紝涓嶅簲璇ュ寘鍚綘鐨勭湡瀹炶蹇嗐€佺湡瀹炶矾寰勩€丄PI key銆佺浜洪」鐩悕鎴栬亰澶╁師鏂囥€?

## 瀹冭В鍐充粈涔堥棶棰?

- 璁?Codex 姣忔寮€濮嬮噸瑕佷换鍔℃椂锛屽厛璇绘渶鐩稿叧鐨勯暱鏈熻蹇嗐€?
- 璁╂瘡娆′换鍔＄粨鏉熸椂锛屾妸绋冲畾浜嬪疄銆侀」鐩姸鎬併€佸伐浣滄祦鍜?Agent 缁忛獙娌夋穩鍒?Markdown銆?
- 璁?Markdown 浠嶇劧鏄簮鏂囦欢锛孲QLite 鍙仛绱㈠紩鍜屾悳绱紝Obsidian 鍙槸鍙€夌殑鏌ョ湅鍜岀紪杈戞柟寮忋€?
- 鍙€夊鍔犲悜閲忔绱細鍙寰楀ぇ姒傛剰鎬濇椂锛岀敤 embedding + Zvec 鎵惧埌鐩稿叧 Markdown锛屽啀鍥炶鍘熸枃銆?
- 鎶婄湡瀹炰俊鎭暀鍦ㄦ湰鍦扮鏈?vault锛屾ā鏉垮彧鎻愪緵缁撴瀯鍜屾柟娉曘€?

## 鏄惁蹇呴』瀹夎 Obsidian锛?

涓嶅繀椤汇€?

杩欎釜椤圭洰鏈川涓婃槸涓€涓?Markdown 鏂囦欢澶?+ SQLite 绱㈠紩鑴氭湰銆備綘鍙互鐩存帴鐢?Codex銆乂S Code 鎴栦换鎰忔枃鏈紪杈戝櫒绠＄悊瀹冦€?

濡傛灉浣犳兂鐢ㄦ洿鑸掓湇鐨勭瑪璁扮晫闈㈡煡鐪嬨€佺紪杈戝拰鎼滅储杩欎簺 Markdown 鏂囦欢锛屽彲浠ュ畨瑁?Obsidian锛岀劧鍚庢妸鐢熸垚鍑烘潵鐨勮蹇嗗簱鏂囦欢澶逛綔涓轰竴涓?Obsidian vault 鎵撳紑銆?

## 鏍稿績缁撴瀯

```text
templates/vault/
  AGENTS.md              # Codex 璇诲彇鍜屽啓鍏ヨ鍒?
  INDEX.md               # 璁板繂璺敱绱㈠紩
  鐢ㄦ埛璁板繂/              # 鐢ㄦ埛鍋忓ソ銆佽竟鐣屻€侀暱鏈熺敾鍍?
  椤圭洰/                  # 椤圭洰绾х姸鎬佸拰缁撹
  宸ヤ綔娴?                # 鍙鐢ㄦ祦绋嬨€佸瓧娈佃鑼冦€佹敹灏捐鍒?
  鍐崇瓥/                  # 鏉冭　鍜屽彇鑸?
  agent/                 # Agent case銆乻kill 鍊欓€夈€佹湭闂幆浜嬮」

scripts/
  bootstrap.py           # 浠庢ā鏉垮垱寤烘湰鍦扮鏈?vault
  agent_memory_index.py  # 鍏ㄥ簱 SQLite 绱㈠紩鍜屾悳绱?
  agent_memory_search.py # 缁熶竴鎼滅储鍏ュ彛锛歋QLite + 鍙€?Zvec + 鎵嬪姩 rg
  agent_memory_closeout.py
                          # 浠诲姟缁撴潫鏀跺熬锛氭鏌ャ€佸璐︺€佸埛鏂扮储寮曘€佸璁°€佸彲閫夋彁浜?
  agent_memory_audit.py  # 瀹氭湡浣撴锛氳繃鏈熴€侀噸澶嶃€乷pen-loop銆佽鍐宠褰?
  agent_memory_audit_autorun.py
                          # audit 鑷姩瑙﹀彂鍣細瓒呰繃闂撮殧鎵嶈繍琛?
  agent_memory_doctor.py  # 鍏ㄩ摼璺綋妫€锛歁arkdown/SQLite/FTS/Zvec/Git/鑷姩鍖?
  agent_memory_stop_hook.py
                          # Stop 浜嬩欢鑺傛祦鎻愰啋 + 鍒版湡 audit
  agent_memory_zvec_index.py
  agent_memory_retrieval_benchmark.py
  agent_evolution.py
  agent_memory_check.py
```

## 蹇€熷紑濮?

```bash
git clone https://github.com/mcncarl/codex-memory.git
cd codex-memory
cp .env.example .env
```

缂栬緫 `.env`锛屾妸 `AGENT_MEMORY_ROOT` 鏀规垚浣犵殑鏈湴璁板繂搴撹矾寰勩€傚畠鍙互鍙槸涓€涓櫘閫氭枃浠跺す锛涘鏋滀綘浣跨敤 Obsidian锛屼篃鍙互鎶婅繖涓枃浠跺す浣滀负 Obsidian vault 鎵撳紑銆?

```bash
python3 scripts/bootstrap.py --memory-root "$HOME/codex-memory-vault" --write-env
source .env
python3 scripts/agent_evolution.py --init --scan --report
python3 scripts/agent_memory_index.py --init --scan --report
python3 scripts/agent_memory_check.py
python3 scripts/agent_memory_doctor.py
```

鎼滅储绀轰緥锛?

```bash
python3 scripts/agent_memory_search.py "椤圭洰 鏀跺熬" --limit 5
python3 scripts/agent_memory_search.py "鍋忓ソ" --track user
python3 scripts/agent_memory_search.py "澶嶇敤娴佺▼" --memory-type workflow
```

浠诲姟缁撴潫鏃跺缓璁娇鐢ㄧ粺涓€鏀跺熬鑴氭湰銆傚畠浼氳嚜鍔ㄥ彂鐜版湭鎻愪氦鍙樻洿锛屼篃浼氳拷韪€滀笂娆℃垚鍔?closeout 瑙傚療鍒扮殑鎻愪氦鈥濅箣鍚庣殑 Git 鍘嗗彶锛屽洜姝?Obsidian Git 绛夊伐鍏锋彁鍓嶈嚜鍔ㄦ彁浜や篃涓嶄細閫犳垚婕忓鐞嗐€傞殢鍚庢墽琛岀粨鏋勬鏌ャ€佸瓧闈笌璇箟鍙岄噸瀵硅处銆丼QLite 鍒锋柊銆佸彲閫?Zvec 琛ユ紡/娓呯悊銆丄gent evolution 鍒锋柊锛屽苟鍦?audit 瓒呰繃闂撮殧鏃堕『鎵嬭窇涓€娆′綋妫€銆傚苟鍙?closeout 浼氳鏂囦欢閿佹嫤浣忥紝閬垮厤鏁版嵁搴撳拰 Git 鍩虹嚎浜掔浉韪╄笍銆?

```bash
python3 scripts/agent_memory_closeout.py --dry-run
python3 scripts/agent_memory_closeout.py --commit
```

鍐欏叆姝ｅ紡璁板繂鍓嶏紝鍙互鍏堣鑴氭湰鍋氫竴娆″璐︼紝鍒ゆ柇搴旇鏂板缓銆佹洿鏂版棫鏂囦欢銆佽烦杩囥€佽繕鏄渶瑕佷汉宸ュ悎骞讹細

```bash
python3 scripts/agent_memory_closeout.py --prewrite "鍑嗗鍐欏叆鐨勮蹇嗘憳瑕?
```

audit 鍙互鎵嬪姩杩愯锛屼篃鍙互鐢?closeout 鎹庡甫瑙﹀彂锛?

```bash
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_audit_autorun.py --reason manual --json
```

鍏ㄩ摼璺仴搴锋鏌ワ細

```bash
python3 scripts/agent_memory_doctor.py
python3 scripts/agent_memory_doctor.py --repair-derived  # 鍙噸寤烘淳鐢熺储寮曪紝涓嶆敼 Markdown
```

鍙€夌殑 Stop hook 涓?macOS `launchd` 鍛ㄦ湡鍏滃簳瑙?[docs/automation.md](docs/automation.md)銆?

## 鍙€夛細璇箟妫€绱?

SQLite 閫傚悎鍏抽敭璇嶆槑纭殑闂锛涘悜閲忔绱㈤€傚悎鈥滃彧璁板緱鎰忔€濓紝涓嶈寰楀師璇嶁€濈殑闂銆傝繖涓ā鏉挎妸璇箟妫€绱㈠仛鎴愬彲閫夋梺璺紝涓嶆浛浠?Markdown 鍜?SQLite銆?

瀹夎鍙€変緷璧栵細

```bash
python3 -m venv "$HOME/.config/codex-memory/.venv"
"$HOME/.config/codex-memory/.venv/bin/python" -m pip install -U pip
"$HOME/.config/codex-memory/.venv/bin/python" -m pip install -r requirements-vector.txt
```

榛樿 embedding 妯″瀷鏄?`google/embeddinggemma-300m`銆傚鏋滀娇鐢?gated 妯″瀷锛岄渶瑕佸厛鍦?Hugging Face 鎺ュ彈妯″瀷鏉℃骞跺畬鎴愭湰鏈虹櫥褰曘€傛ā鍨嬬紦瀛樺拰鍚戦噺搴撻兘鍙簲淇濆瓨鍦ㄦ湰鍦帮紝涓嶈鎻愪氦鍒板叕寮€浠撳簱銆?

```bash
python3 scripts/agent_memory_index.py --init --scan --report
"$HOME/.config/codex-memory/.venv/bin/python" scripts/agent_memory_zvec_index.py --init
"$HOME/.config/codex-memory/.venv/bin/python" scripts/agent_memory_zvec_index.py --scan --prune
"$HOME/.config/codex-memory/.venv/bin/python" scripts/agent_memory_zvec_index.py --report
"$HOME/.config/codex-memory/.venv/bin/python" scripts/agent_memory_zvec_index.py --search "鍙寰楀ぇ姒傛剰鎬濈殑闂"
```

瀵规瘮 SQLite 鍜屽悜閲忔绱細

```bash
"$HOME/.config/codex-memory/.venv/bin/python" scripts/agent_memory_retrieval_benchmark.py --limit 5
```

## 璁捐鍘熷垯

1. Markdown 鏄簨瀹炴簮锛孲QLite 鏄储寮曘€?
2. 鏅€氳蹇嗙洿鎺ヨ繘鍏ユ寮忕洰褰曪紝涓嶅仛鏃犳剰涔夊€欓€夋睜銆?
3. Agent 鑷垜杩涘寲鍗曠嫭鏀惧湪 `agent/`锛屽叾涓?case 鍜?skill 鍊欓€夌敤浜庡鐢ㄧ粡楠屾矇娣€銆?
4. 鐢ㄦ浜ゅ瓧娈佃繃婊よ蹇嗭細`user_id`銆乣agent_id`銆乣app_id`銆乣project_id`銆乣session_id`銆乣track`銆乣memory_type`銆乣status`銆?
5. 璇箟妫€绱㈠彧浣滀负鍊欓€夊彫鍥炲眰锛屾渶缁堢瓟妗堝繀椤诲洖璇?Markdown 鍘熸枃銆?
6. closeout 璐熻矗鈥滀换鍔＄粨鏉熷悗鐨勮嚜鍔ㄦ暣鐞嗏€濓紝audit 璐熻矗鈥滃畾鏈熷彂鐜拌澶嶆牳銆佸悎骞舵垨蹇界暐鐨勮蹇嗏€濓紝浣嗕簩鑰呴兘涓嶈嚜鍔ㄦ敼鍐欎簨瀹炲眰銆?
7. API key銆佹ā鍨嬬紦瀛樸€丼QLite銆乤udit 瑁佸喅搴撳拰鍚戦噺搴撳彧鏀炬湰鍦帮紝姘歌繙涓嶅啓杩?Markdown 璁板繂鍜屽叕寮€浠撳簱銆?
8. `verified_at` 蹇呴』鍖哄垎鐪熷疄澶嶆牳涓庢枃浠?mtime 鍥為€€锛涗笉鍚岃蹇嗙被鍨嬬敤 `review_after_days` 璁剧疆涓嶅悓澶嶆牳鍛ㄦ湡銆?
9. 缁熶竴鎼滅储浼氬悓鏃跺悎骞跺叧閿瘝涓庤涔夌粨鏋滐紝鎵€鏈夌瓫閫夊湪鍚堝苟鍚庡啀娆℃墽琛岋紝骞剁敤璺濈闃堝€兼嫆缁濃€滅‖鍑戝嚭鏉モ€濈殑鏃犲叧杩戦偦銆?

## 鑷磋阿

鏈」鐩殑閮ㄥ垎璁捐鎬濊矾鍙?[EverOS](https://github.com/EverMind-AI/EverOS) 鍚彂锛岃瑙?[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)銆?
