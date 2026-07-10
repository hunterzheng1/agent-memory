# Agent Memory Instructions

杩欐槸鏈湴闀挎湡璁板繂搴撱€傞亣鍒版棦鏈夐」鐩€佷粨搴撱€佽矾寰勩€佷汉鐗┿€佸巻鍙茬粨璁恒€佺户缁笂娆′换鍔°€佹姤鍛娿€佽皟鐮斻€佽緝闀挎帓鏌ユ椂锛岄粯璁ゅ厛浣跨敤杩欎釜璁板繂搴擄紱绠€鍗曠炕璇戙€佹敼涓€鍙ヨ瘽銆佹煡鏃堕棿绛変竴娆℃€у皬浠诲姟鍙互璺宠繃銆?

璇诲彇椤哄簭锛?

1. 鍏堣鏈枃浠躲€?
2. 鍐嶈 `INDEX.md`銆?
3. 鏍规嵁浠诲姟鍏抽敭璇嶏紝鍙鏈€鐩稿叧鐨?1-3 涓枃浠躲€?

涓嶈榛樿璇诲彇鏁翠釜璁板繂搴撱€?

## 妫€绱㈣鍒?

浼樺厛浣跨敤缁熶竴鎼滅储鑴氭湰锛岃€屼笉鏄墜宸ョ寽璇ヨ鍝釜鏂囦欢锛?

```bash
python3 scripts/agent_memory_search.py "鏌ヨ璇? --limit 5
```

瀹冧細鍏堟煡 SQLite/FTS锛涘惎鐢ㄨ涔夌储寮曟椂锛屼篃鍙互骞惰鏌?Zvec銆俍vec 鍛戒腑鍙兘褰撲綔鍊欓€夌嚎绱紝鏈€缁堝洖绛斿墠蹇呴』鍥炶 Markdown 鍘熸枃銆?

## 鍐欏叆瑙勫垯

姝ｅ紡鍐欏叆鍓嶅厛鍋氬璐︼紝閬垮厤閲嶅璁板繂瓒婂啓瓒婂锛?

```bash
python3 scripts/agent_memory_closeout.py --prewrite "鍑嗗鍐欏叆鐨勮蹇嗘憳瑕?
```

瀵硅处鍔ㄤ綔鍙厑璁歌繖 6 绉嶏細

- `ADD`锛氭柊寤鸿蹇嗐€?
- `UPDATE`锛氭洿鏂板凡鏈夎蹇嗐€?
- `NOOP`锛氫笉鍐欍€?
- `MARK_OUTDATED`锛氭棫淇℃伅杩囨椂锛屼絾涓嶅垹闄ゃ€?
- `MERGE_REQUIRED`锛氱枒浼奸噸澶嶆垨鍐茬獊锛岄渶瑕佷汉宸ュ悎骞躲€?
- `ASK_USER`锛氭秹鍙婃晱鎰熴€佸垹闄ゃ€佽垂鐢ㄣ€佽处鍙枫€佸嚟璇佹垨涓嶇‘瀹氬垽鏂椂鍏堥棶鐢ㄦ埛銆?

閲嶈浠诲姟缁撴潫鍓嶆墽琛?memory closeout锛?

```bash
python3 scripts/agent_memory_closeout.py --dry-run
python3 scripts/agent_memory_closeout.py --commit
```

closeout 浼氳嚜鍔ㄥ彂鐜拌蹇嗗簱鍙樻洿鏂囦欢锛屾墽琛岀粨鏋勬鏌ャ€佸啓鍏ュ悗瀵硅处銆丼QLite 鍒锋柊銆佸彲閫?Zvec 鍒锋柊銆丄gent evolution 鍒锋柊銆乤udit 鎹庡甫瑙﹀彂銆乧loseout 鏃ュ織鍐欏叆锛屽苟鍙彁浜ゆ湰杞鐞嗚繃鐨勮蹇嗘枃浠躲€?

濡傛灉 closeout 杈撳嚭 `MERGE_REQUIRED`銆乣ASK_USER`銆佸垹闄ゆ枃浠剁姸鎬併€佺枒浼煎巻鍙茶剰鍙樻洿锛屽厛鍋滀笅璁╃敤鎴风‘璁ゃ€?

鏅€氳蹇嗙洿鎺ュ啓鍏ユ寮忕洰褰曪細`鐢ㄦ埛璁板繂/`銆乣椤圭洰/`銆乣宸ヤ綔娴?`銆乣鍐崇瓥/`銆侫gent 澶嶇敤缁忛獙鍐欏叆 `agent/cases/` 鎴?`agent/case-candidates/`銆傚娆″鐢ㄣ€佸彲鎶借薄鎴愭祦绋嬬殑缁忛獙锛屽啓鍏?`agent/skill-candidates/`锛屾寮忓崌绾?skill 鍓嶉渶瑕佺敤鎴风‘璁ゃ€?

## Audit 瑙勫垯

audit 鐢ㄦ潵鍙戠幇闇€瑕佸鏍搞€佸悎骞舵垨蹇界暐鐨勮蹇嗭紝涓嶇洿鎺ユ敼鍐?Markdown 浜嬪疄灞傘€?

```bash
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_audit.py --ignore FINDING_ID --note "淇濈暀鍘熷洜"
python3 scripts/agent_memory_audit_autorun.py --reason closeout --min-interval-days 7
python3 scripts/agent_memory_doctor.py
```

鎺ㄨ崘璁?closeout 姣?7 澶╂崕甯︽鏌ヤ竴娆?audit 鏄惁璇ヨ繍琛屻€俛udit findings 搴旇鐢辩敤鎴锋垨 Agent 鏄庣‘瑁佸喅锛岄伩鍏嶆姤鍛婃湰韬彉鎴愭柊鐨?open-loop 鍣０銆?

## 瀛楁瑕佹眰

鏂板缓鎴栭噸鍐欐寮忚蹇嗘椂锛屽敖閲忓寘鍚笅闈㈠瓧娈碉細

```yaml
---
memory_type: project
track: project
project_id: example-app
app_id: {{APP_ID}}
user_id: {{USER_ID}}
agent_id: {{AGENT_ID}}
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
review_after_days: 90
keywords:
  - example
---
```

## 瀹夊叏杈圭晫

- 涓嶈鎶?API key銆乼oken銆乧ookie銆佸瘑鐮佸啓鍏?Markdown銆?
- 涓嶈鎶婄瀵嗗師濮嬭亰澶╁叏鏂囧啓鍏ュ叕寮€浠撳簱銆?
- 涓嶈鎶?SQLite 鏁版嵁搴撴彁浜ゅ埌 Git銆?
- 鎼滅储鏃ュ織鍙繚瀛樻煡璇㈠搱甯屻€侀暱搴︺€佹潵婧愬拰鑰楁椂锛屼笉淇濆瓨鏂扮殑鏌ヨ鍘熸枃銆?
- 瀵瑰鍒嗕韩鍓嶅繀椤昏劚鏁忋€?
