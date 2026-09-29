# -*- coding: utf-8 -*-
"""
Quantum Wire —— 信息采集器（MVP）

三层交叉架构：
  发现层  Google News RSS（按企业名 + 行业关键词）
  核实层  SEC EDGAR（上市公司申报，最稳） + PR Newswire（稿件分发）
  兜底层  只有发现层线索、核实层没拿到的，标记 pending，不上站

输出：data/items.json（可直接被前端消费）
纯标准库，无第三方依赖，可在 GitHub Actions 免费 runner 上跑。
"""
import json
import re
import ssl
import gzip
import html
import time
import hashlib
import argparse
import io
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus, urljoin
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

UA = ("QuantumWireBot/0.1 (quantum industry index; contact: ops@example.com) "
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "site", "data")

# 国家 / 地区：[中文名, 英文名, 徽章色]
COUNTRIES = {
    "US": ["美国", "United States", "#3B6FD4"], "CN": ["中国", "China", "#D64545"],
    "TW": ["中国台湾", "Taiwan, China", "#D64545"], "GB": ["英国", "United Kingdom", "#5B8DEF"],
    "CA": ["加拿大", "Canada", "#E05555"], "FR": ["法国", "France", "#6C7BE0"],
    "DE": ["德国", "Germany", "#4A4A8A"], "NL": ["荷兰", "Netherlands", "#E08A3C"],
    "FI": ["芬兰", "Finland", "#4FA8C7"], "CH": ["瑞士", "Switzerland", "#C74B4B"],
    "AT": ["奥地利", "Austria", "#B05555"], "ES": ["西班牙", "Spain", "#D98A3C"],
    "DK": ["丹麦", "Denmark", "#C2504F"], "JP": ["日本", "Japan", "#C2506E"],
    "KR": ["韩国", "South Korea", "#4A7EC7"], "SG": ["新加坡", "Singapore", "#3CA89A"],
    "IN": ["印度", "India", "#E08A4C"], "IL": ["以色列", "Israel", "#3C7EC7"],
    "AU": ["澳大利亚", "Australia", "#3CA87E"], "SA": ["沙特", "Saudi Arabia", "#4A9E7E"],
}

# 技术参数抽取规则 —— 只在明确上下文里取值，宁可漏抽也不要误抽
# 形如 "256 Platform" 这类产品型号不会被当作比特数，因为要求单位紧跟数字
SPEC_RULES = [
    # 逻辑比特要求数字与「logical qubits」紧邻，不做宽松前瞻，否则极易误抽
    ("logical_qubits", [r"\b(\d{1,3})\s*logical\s+qubits?\b"], "int"),
    ("qubits", [r"\b(\d{1,4})\s*(?:physical\s+)?qubits?\b",
                r"\b(\d{1,4})\s*[-–]\s*qubit\b",
                r"\b(\d{1,4})\s*qubit\s+(?:system|processor|computer|machine|chip|device|processor)\b",
                r"\b(\d{1,4})\s*(?:qubit|QPU)\s+(?:quantum\s+)?(?:computer|processor|system)\b"], "int"),
    ("fidelity", [r"(?:fidelity|保真度)[^\d]{0,24}?(\d{2}(?:\.\d+)?)\s*%",
                  r"(\d{2}(?:\.\d+)?)\s*%\s*(?:two-?qubit\s+|gate\s+)?fidelity"], "float"),
    ("error_rate", [r"error\s+rate[^\d]{0,24}?(\d+(?:\.\d+)?)\s*%",
                    r"(\d+(?:\.\d+)?)\s*%\s*error\s+rate"], "float"),
    ("amount", [r"([$€£])\s?(\d+(?:\.\d+)?)\s*(billion|million|bn|m\b|b\b)",
                r"\b(usd|eur|gbp)\s?(\d+(?:\.\d+)?)\s*(billion|million|bn|m\b|b\b)",
                r"\b(\d+(?:\.\d+)?)\s*(million|billion)\s*(?:usd|dollars?|euros?)\b"], "money"),
]
MONEY_MULT = {"billion": 1e9, "b": 1e9, "bn": 1e9, "million": 1e6, "m": 1e6}
CURMAP = {"usd": "$", "eur": "€", "gbp": "£"}


def extract_specs(title, desc="", ctx=False):
    """从文本抽取技术参数。

    返回 {key: value}；ctx=True 时额外返回 {key: 原文上下文片段}，
    用于让读者判断数值的真实语境（例如「模拟规模」还是「实际硬件规格」）。
    """
    blob = (title or "") + " " + (desc or "")
    out = {}
    ctxs = {}
    for key, pats, kind in SPEC_RULES:
        if key in out:
            continue
        m = None
        for pat in pats:
            m = re.search(pat, blob, re.I)
            if m:
                break
        if not m:
            continue
        if ctx:
            a = max(0, m.start() - 85)
            ctxs[key] = blob[a:m.end() + 85].strip()
        try:
            if kind == "money":
                raw_cur = (m.group(1) or "").strip()
                cur = CURMAP.get(raw_cur.lower(), raw_cur if raw_cur in "$€£" else "$")
                # 第三种模式：数字在前、货币码在后，分组顺序不同
                if re.match(r"^\d", raw_cur):
                    num, unit = float(raw_cur), m.group(2).lower()
                else:
                    num, unit = float(m.group(2)), m.group(3).lower()
                mult = MONEY_MULT.get(unit)
                if not mult:
                    continue
                out[key] = {"currency": cur, "value": num * mult, "raw": m.group(0).strip()}
            elif kind == "int":
                out[key] = int(m.group(1))
            else:
                v = float(m.group(1))
                if key == "fidelity" and v < 50:   # 保真度不可能低于 50%，多半是误抽
                    continue
                out[key] = v
        except (ValueError, TypeError):
            continue
    return (out, ctxs) if ctx else out


# 上站分级：official = 企业一手发布（官网 / SEC / 通讯社稿件）；media = 媒体线索
SOURCE_TIER = {"企业官网": "official", "SEC EDGAR": "official", "PR Newswire": "official",
               "Google News": "media"}

# ---------------------------------------------------------------- 企业档案
# key: 企业中文名  v: [缩写, 色, 技术路线, 国家码, 城市, 英文名/别名(用于匹配)]
COMPANIES = {
    "IonQ": ["IONQ", "#5B8DEF", "离子阱", "US", "马里兰", "ionq"],
    "Rigetti Computing": ["RGTI", "#F59E0B", "超导", "US", "加州", "rigetti"],
    "D-Wave Quantum": ["QBTS", "#F25A5A", "量子退火", "US", "加州", "d-wave|dwave"],
    "Quantum Computing Inc": ["QUBT", "#34D399", "光量子", "US", "新泽西", "quantum computing inc"],
    "SEALSQ": ["LAES", "#4ADE80", "后量子密码", "CH", "日内瓦", "sealsq"],
    "Arqit Quantum": ["ARQQ", "#F25A5A", "后量子密码", "GB", "伦敦", "arqit"],
    "IQM Quantum Computers": ["IQM", "#5EEAD4", "超导", "FI", "埃斯波", "iqm"],
    "Infleqtion": ["INFQ", "#60A5FA", "中性原子", "US", "科罗拉多", "infleqtion|coldquanta"],
    "Quantinuum": ["QTNM", "#C084FC", "离子阱", "GB", "剑桥", "quantinuum"],
    "Pasqal": ["PASQ", "#A78BFA", "中性原子", "FR", "马西", "pasqal"],
    "QuEra Computing": ["QUERA", "#22D3EE", "中性原子", "US", "波士顿", "quera"],
    "PsiQuantum": ["PSIQ", "#818CF8", "光量子", "US", "加州", "psiquantum|psiquantum"],
    "SandboxAQ": ["SBAQ", "#F472B6", "后量子密码", "US", "加州", "sandboxaq"],
    "Xanadu": ["XAN", "#F472B6", "光量子", "CA", "多伦多", "xanadu"],
    "IBM Quantum": ["IBM", "#38BDF8", "超导", "US", "纽约州", "ibm quantum|ibm's quantum"],
    "Google Quantum AI": ["GOOGL", "#FBBF24", "超导", "US", "加州", "google quantum|google ai quantum|google willow|alphabet.*quantum"],
    "Microsoft Quantum": ["MSFT", "#60A5FA", "拓扑", "US", "华盛顿州", "microsoft quantum|azure quantum|microsoft's quantum"],
    "Quantum Machines": ["QM", "#818CF8", "软件算法", "IL", "特拉维夫", "quantum machines"],
    "Quandela": ["QDEL", "#38BDF8", "光量子", "FR", "马西", "quandela"],
    "Alice & Bob": ["AAB", "#F472B6", "超导", "FR", "巴黎", "alice"],
    "Oxford Ionics": ["OXION", "#22D3EE", "离子阱", "GB", "牛津", "oxford ionics"],
    "ORCA Computing": ["ORCA", "#4ADE80", "光量子", "GB", "伦敦", "orca computing"],
    "Silicon Quantum Computing": ["SQC", "#3CA87E", "超导", "AU", "悉尼", "silicon quantum"],
    "Quantum Brilliance": ["QBR", "#22D3EE", "超导", "AU", "堪培拉", "quantum brilliance"],
    "Diraq": ["DIRAQ", "#5B8DEF", "超导", "AU", "悉尼", "diraq"],
    "Q-CTRL": ["QCTRL", "#A78BFA", "软件算法", "AU", "悉尼", "q-ctrl|qctrl"],
    "Classiq": ["CLSQ", "#5EEAD4", "软件算法", "IL", "特拉维夫", "classiq"],
    "Fujitsu": ["FJTS", "#C2506E", "超导", "JP", "东京", "fujitsu|富士通"],
    "NEC": ["NEC", "#4A7EC7", "超导", "JP", "东京", "nec quantum|nec corporation"],
    "Toshiba": ["TOSH", "#5B8DEF", "后量子密码", "JP", "东京", "toshiba|东芝"],
    "OptQC": ["OPTQC", "#4ADE80", "光量子", "JP", "东京", "optqc"],
    "Jij": ["JIJ", "#FBBF24", "软件算法", "JP", "东京", "jij inc|jij, inc"],
    "Samsung Electronics": ["SMSN", "#4A7EC7", "超导", "KR", "首尔", "samsung|三星"],
    "SK Telecom": ["SKT", "#F59E0B", "后量子密码", "KR", "首尔", "sk telecom"],
    "本源量子": ["ORIGQ", "#FB923C", "超导", "CN", "合肥", "origin quantum|本源"],
    "国盾量子": ["CTEK", "#4ADE80", "后量子密码", "CN", "合肥", "quantumctek|国盾"],
    "图灵量子": ["TURQ", "#38BDF8", "光量子", "CN", "上海", "turingq|图灵量子"],
    "玻色量子": ["BOSON", "#F472B6", "光量子", "CN", "北京", "boson quantum|玻色"],
    "Multiverse Computing": ["MVC", "#D98A3C", "软件算法", "ES", "圣塞巴斯蒂安", "multiverse computing"],
    "QuantWare": ["QWARE", "#F59E0B", "超导", "NL", "代尔夫特", "quantware"],
    "Qblox": ["QBLOX", "#22D3EE", "软件算法", "NL", "代尔夫特", "qblox"],
    "planqc": ["PLANQ", "#5EEAD4", "中性原子", "DE", "慕尼黑", "planqc"],
    "AQT": ["AQT", "#C74B4B", "离子阱", "AT", "因斯布鲁克", "alpine quantum"],
    "ID Quantique": ["IDQ", "#C74B4B", "后量子密码", "CH", "日内瓦", "id quantique"],
    "Terra Quantum": ["TERRA", "#3CA87E", "软件算法", "CH", "圣加仑", "terra quantum"],
    "Photonic Inc": ["PHOT", "#22D3EE", "光量子", "CA", "温哥华", "photonic inc"],
    "Anyon Systems": ["ANYON", "#5B8DEF", "超导", "CA", "蒙特利尔", "anyon systems"],
    "Nord Quantique": ["NORDQ", "#4ADE80", "超导", "CA", "舍布鲁克", "nord quantique"],
}

# 第二批：档案库补全（尚未接入官网直抓，用于档案页与国家页收录）
COMPANIES.update({
    "AWS Quantum": ["AWS", "#F59E0B", "超导", "US", "华盛顿州", "aws quantum|amazon braket"],
    "NVIDIA Quantum": ["NVDA", "#4ADE80", "软件算法", "US", "加州", "nvidia quantum|cuda quantum"],
    "Atom Computing": ["ATOM", "#A78BFA", "中性原子", "US", "加州", "atom computing"],
    "SEEQC": ["SEEQC", "#5EEAD4", "超导", "US", "纽约州", "seeqc"],
    "Qolab": ["QOLAB", "#94A3B8", "超导", "US", "加州", "qolab"],
    "Strangeworks": ["STGW", "#C084FC", "软件算法", "US", "德州", "strangeworks"],
    "QC Ware": ["QCW", "#38BDF8", "软件算法", "US", "加州", "qc ware"],
    "Zapata Quantum": ["ZPTA", "#FBBF24", "软件算法", "US", "波士顿", "zapata"],
    "Qunnect": ["QUNN", "#22D3EE", "后量子密码", "US", "纽约", "qunnect"],
    "QuSecure": ["QSEC", "#4ADE80", "后量子密码", "US", "加州", "qusecure"],
    "Atlantic Quantum": ["ATQ", "#5B8DEF", "超导", "US", "波士顿", "atlantic quantum"],
    "Qrypt": ["QRYP", "#A78BFA", "后量子密码", "US", "纽约", "qrypt"],
    "Quantum Xchange": ["QXC", "#F472B6", "后量子密码", "US", "马里兰", "quantum xchange"],

    "华翊量子": ["HYQ", "#A78BFA", "离子阱", "CN", "北京", "华翊"],
    "中科酷原": ["ZKKY", "#5EEAD4", "中性原子", "CN", "武汉", "酷原"],
    "问天量子": ["WTQ", "#FBBF24", "后量子密码", "CN", "芜湖", "问天量子"],
    "启科量子": ["QIKE", "#60A5FA", "离子阱", "CN", "北京", "启科量子"],
    "国仪量子": ["CIQTEK", "#94A3B8", "软件算法", "CN", "合肥", "国仪量子"],
    "中电信量子集团": ["CTQL", "#3B6FD4", "后量子密码", "CN", "合肥", "中国电信量子|中电信量子"],
    "百度量子计算研究所": ["BAIDU", "#5B8DEF", "超导", "CN", "北京", "百度量子"],
    "腾讯量子实验室": ["TXQ", "#4ADE80", "软件算法", "CN", "深圳", "腾讯量子"],
    "华为量子": ["HWQ", "#F25A5A", "超导", "CN", "深圳", "华为量子"],
    "量羲技术": ["LXQ", "#22D3EE", "软件算法", "CN", "合肥", "量羲"],
    "中国电子科技集团": ["CETC", "#D64545", "后量子密码", "CN", "北京", "中国电科|cetc"],
    "合肥国家实验室": ["HFNL", "#818CF8", "超导", "CN", "合肥", "合肥国家实验室"],
    "鸿海研究院": ["HHR", "#94A3B8", "超导", "TW", "新北", "鸿海研究院|hon hai"],

    "Oxford Quantum Circuits": ["OQC", "#5B8DEF", "超导", "GB", "牛津", "oxford quantum circuits|oqc"],
    "Riverlane": ["RIVL", "#38BDF8", "软件算法", "GB", "剑桥", "riverlane"],
    "Phasecraft": ["PHSC", "#A78BFA", "软件算法", "GB", "布里斯托", "phasecraft"],
    "KETS Quantum Security": ["KETS", "#F59E0B", "后量子密码", "GB", "布里斯托", "kets quantum"],
    "Nu Quantum": ["NUQ", "#F472B6", "后量子密码", "GB", "剑桥", "nu quantum"],
    "quantumDice": ["QDICE", "#5EEAD4", "后量子密码", "GB", "牛津", "quantumdice"],
    "Quantum Motion": ["QMOT", "#94A3B8", "超导", "GB", "伦敦", "quantum motion"],
    "Universal Quantum": ["UNIQ", "#818CF8", "离子阱", "GB", "布莱顿", "universal quantum"],

    "1QBit": ["1QBIT", "#A78BFA", "软件算法", "CA", "温哥华", "1qbit"],
    "EvolutionQ": ["EVOQ", "#F59E0B", "后量子密码", "CA", "滑铁卢", "evolutionq"],
    "SBQuantum": ["SBQ", "#94A3B8", "软件算法", "CA", "魁北克", "sbquantum"],

    "C12 Quantum Electronics": ["C12", "#5EEAD4", "超导", "FR", "巴黎", "c12 quantum"],
    "Cryptonext Security": ["CNXT", "#F25A5A", "后量子密码", "FR", "巴黎", "cryptonext"],
    "VeriQloud": ["VERIQ", "#818CF8", "后量子密码", "FR", "巴黎", "veriqloud"],
    "Atos Quantum": ["ATOS", "#94A3B8", "软件算法", "FR", "巴黎", "atos quantum"],

    "eleQtron": ["ELEQ", "#5B8DEF", "离子阱", "DE", "锡根", "eleqtron"],
    "Aqarios": ["AQAR", "#FBBF24", "软件算法", "DE", "慕尼黑", "aqarios"],
    "HQS Quantum Simulations": ["HQS", "#A78BFA", "软件算法", "DE", "卡尔斯鲁厄", "hqs quantum"],
    "Black Semiconductor": ["BLKS", "#64748B", "超导", "DE", "亚琛", "black semiconductor"],
    "Qruise": ["QRUI", "#4ADE80", "软件算法", "DE", "亚琛", "qruise"],

    "Bluefors": ["BLUE", "#38BDF8", "软件算法", "FI", "赫尔辛基", "bluefors"],
    "SemiQon": ["SEMIQ", "#94A3B8", "超导", "FI", "埃斯波", "semiqon"],

    "QuiX Quantum": ["QUIX", "#F472B6", "光量子", "NL", "恩斯赫德", "quix quantum"],
    "OrangeQS": ["OQS", "#FB923C", "软件算法", "NL", "代尔夫特", "orangeqs"],
    "Delft Circuits": ["DELFT", "#5B8DEF", "软件算法", "NL", "代尔夫特", "delft circuits"],
    "Single Quantum": ["SQNT", "#4ADE80", "光量子", "NL", "代尔夫特", "single quantum"],
    "QphoX": ["QPHOX", "#A78BFA", "光量子", "NL", "代尔夫特", "qphox"],

    "ParityQC": ["PARQ", "#818CF8", "软件算法", "AT", "因斯布鲁克", "parityqc"],
    "Qnami": ["QNAMI", "#94A3B8", "软件算法", "CH", "巴塞尔", "qnami"],
    "Qilimanjaro Quantum Tech": ["QILI", "#4ADE80", "超导", "ES", "巴塞罗那", "qilimanjaro"],
    "Kvantify": ["KVANT", "#C2504F", "软件算法", "DK", "哥本哈根", "kvantify"],
    "Sparrow Quantum": ["SPAR", "#38BDF8", "光量子", "DK", "哥本哈根", "sparrow quantum"],

    "NTT": ["NTT", "#94A3B8", "光量子", "JP", "东京", "ntt"],
    "Quemix": ["QUEM", "#A78BFA", "软件算法", "JP", "东京", "quemix"],
    "Fixstars Amplify": ["FIXS", "#22D3EE", "量子退火", "JP", "东京", "fixstars"],
    "LG Electronics": ["LG", "#C2506E", "软件算法", "KR", "首尔", "lg electronics"],
    "KT": ["KT", "#94A3B8", "后量子密码", "KR", "首尔", "kt corp"],

    "Horizon Quantum Computing": ["HQC", "#3CA89A", "软件算法", "SG", "新加坡", "horizon quantum"],
    "SpeQtral": ["SPEQ", "#5B8DEF", "后量子密码", "SG", "新加坡", "speqtral"],
    "QpiAI": ["QPIAI", "#E08A4C", "软件算法", "IN", "班加罗尔", "qpiai"],
    "BosonQ Psi": ["BQPSI", "#4ADE80", "软件算法", "IN", "班加罗尔", "bosonq"],
    "QNu Labs": ["QNU", "#F25A5A", "后量子密码", "IN", "班加罗尔", "qnu labs"],

    "Qedma": ["QEDMA", "#F472B6", "软件算法", "IL", "特拉维夫", "qedma"],
    "Quantum Source": ["QSRC", "#38BDF8", "光量子", "IL", "特拉维夫", "quantum source"],
    "LightSolver": ["LTSV", "#FBBF24", "光量子", "IL", "特拉维夫", "lightsolver"],
    "Archer Materials": ["ARCH", "#94A3B8", "超导", "AU", "阿德莱德", "archer materials"],
    "Pasqal Arabia": ["PASA", "#4A9E7E", "中性原子", "SA", "利雅得", "pasqal arabia"],
})

# 上市公司：SEC EDGAR 用（ticker 由 company_tickers.json 动态解析 CIK）
SEC_TICKERS = ["IONQ", "RGTI", "QBTS", "QUBT", "LAES", "ARQQ"]

# 事件类型判定规则（按优先级）
TYPE_RULES = [
    ("财报业绩", r"(results|earnings|quarterly|q[1-4]\s|fy\s?\d|financial results|revenue)"),
    ("融资并购", r"(raise[sd]?|funding|series [a-d]|investment round|acquisition|acquire|merger|combination|ipo|spac)"),
    ("产品发布", r"(launch|unveil|introduc|release[sd]?|announc\w* (?:new|first)|now available|debut)"),
    ("合作签约", r"(partner|collaborat|agreement|memorandum|team(?:s|ing)? up|joint|contract|signs)"),
    ("技术进展", r"(breakthrough|milestone|demonstrat|achiev|benchmark|record|advanc|error correct|fidelity|qubit count)"),
]

# 噪音过滤：这些来源/标题特征不上站
NOISE = re.compile(
    r"(clinical|pharma|drug|fda|patient|therapeutic|biotech|halloween|donate|"
    r"horoscope|lottery|casino|crypto price|stock (?:price|forecast)|buy now|"
    r"press release distribution)", re.I)

QUANTUM_HINT = re.compile(
    r"(quantum|qubit|qkd|post-quantum|pqc|anneal|ion trap|trapped ion|photonic quantum|"
    r"neutral atom|superconducting qubit|量子)", re.I)


# ---------------------------------------------------------------- 工具
def get(url, timeout=20, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/html;q=0.9, */*;q=0.5",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            })
            with urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.2)
    raise last


def clean(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_rss(text):
    """返回 [{'title','link','date','desc','src'}]"""
    out = []
    for m in re.finditer(r"<item\b.*?</item>", text, re.S | re.I):
        b = m.group(0)

        def one(tag):
            mm = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), b, re.S | re.I)
            return clean(mm.group(1)) if mm else ""

        link = ""
        lm = re.search(r"<link[^>]*>(.*?)</link>", b, re.S | re.I)
        if lm:
            link = clean(lm.group(1))
        title = one("title")
        date = one("pubDate")
        desc = one("description")
        out.append({"title": title, "link": link, "date": date, "desc": desc})
    return out


def to_iso(datestr):
    if not datestr:
        return ""
    try:
        dt = parsedate_to_datetime(datestr)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", datestr)
        return m.group(0) if m else ""


def guess_type(title, desc=""):
    t = (title or "") + " " + (desc or "")
    for name, pat in TYPE_RULES:
        if re.search(pat, t, re.I):
            return name
    return "技术进展"


def match_company(title, desc=""):
    blob = (title + " " + desc).lower()
    for name, meta in COMPANIES.items():
        alias = meta[5]
        for a in alias.split("|"):
            if len(a) < 3:
                continue
            if a in blob:
                return name
    return ""


# ---------------------------------------------------------------- 源 1：SEC EDGAR
def sec_fetch(ticker_map, days=45):
    """按 CIK 拉近期申报。ticker_map: {TICKER: (cik_str, company_name)}"""
    items = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    def one(tk):
        info = ticker_map.get(tk)
        if not info:
            return []
        cik, secname = info
        url = "https://data.sec.gov/submissions/CIK%s.json" % cik.zfill(10)
        try:
            j = json.loads(get(url))
        except Exception as e:
            print("  [SEC] %s 失败: %s" % (tk, str(e)[:60]))
            return []
        rec = j.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        dates = rec.get("filingDate", [])
        accs = rec.get("accessionNumber", [])
        prim = rec.get("primaryDocument", [])
        descs = rec.get("primaryDocDescription", [])
        out = []
        if not forms:
            return out
        cik_int = str(int(cik))
        for i, f in enumerate(forms):
            if i >= len(dates) or dates[i] < cutoff:
                continue
            if f not in ("8-K", "6-K", "10-Q", "10-K"):
                continue
            acc = accs[i].replace("-", "")
            doc = prim[i] if i < len(prim) else ""
            link = "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (cik_int, acc, doc)
            title = "%s files %s" % (secname, f)
            desc = descs[i] if i < len(descs) else ""
            out.append({
                "title": ("%s files %s" % (secname, f)) + (" — " + desc if desc else ""),
                "link": link, "date": dates[i], "desc": desc,
                "ticker": tk, "form": f,
            })
        return out

    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(one, list(ticker_map.keys())):
            items.extend(r)
    return items


def sec_ticker_map():
    """下载 ticker->CIK 映射，只保留我们关心的公司"""
    try:
        raw = json.loads(get("https://www.sec.gov/files/company_tickers.json", timeout=25))
    except Exception as e:
        print("  [SEC] ticker 映射下载失败: %s" % str(e)[:60])
        return {}
    want = set(SEC_TICKERS)
    m = {}
    for _, v in raw.items():
        tk = v.get("ticker", "")
        if tk in want:
            m[tk] = (str(v.get("cik_str")), v.get("title", tk))
    return m


# ---------------------------------------------------------------- 源 2：Google News（发现层）
GN_QUERIES = [
    "quantum computing company announcement",
    "quantum computing funding round",
    "quantum computing earnings results",
    "IonQ OR Rigetti OR D-Wave OR Quantinuum OR IQM",
    "Pasqal OR QuEra OR PsiQuantum OR Infleqtion quantum",
    "量子计算 公司 发布",
    "quantum computing China OR Japan OR Korea company",
    "post-quantum cryptography company",
]


def google_news(days=14):
    base = "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
    items = []

    def one(q):
        try:
            txt = get(base % quote_plus(q), timeout=25)
            return parse_rss(txt)
        except Exception as e:
            print("  [GN] %s 失败: %s" % (q[:34], str(e)[:50]))
            return []

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(one, GN_QUERIES):
            items.extend(r)
    return items


# ---------------------------------------------------------------- 源 3：PR Newswire
PR_FEED = "https://www.prnewswire.com/rss/news-releases-list.rss"


def pr_newswire():
    try:
        return parse_rss(get(PR_FEED, timeout=25))
    except Exception as e:
        print("  [PR] 失败: %s" % str(e)[:60])
        return []


# ---------------------------------------------------------------- 源 4：企业官网 Newsroom（一手原文）
# 只收录第一轮实测能稳定抽出条目的站点
SITES = {
    "IonQ": "https://ionq.com/news",
    "D-Wave Quantum": "https://www.dwavequantum.com/company/newsroom/",
    "Pasqal": "https://pasqal.com/news",
    "IQM Quantum Computers": "https://iqm.tech/company/press-releases/",
    "Quantinuum": "https://www.quantinuum.com/news",
    "国盾量子": "https://www.quantum-info.com/",
    "Rigetti Computing": "https://www.rigetti.com/news",
    "QuEra Computing": "https://www.quera.com/news",
    "Infleqtion": "https://infleqtion.com/news/",
    "Quandela": "https://www.quandela.com/news/",
}

NAV_NOISE = re.compile(
    r"^(learn more|see how|explore|get educated|read more|view all|blog|press releases?|"
    r"newsroom|news|media|resources?|home|about|careers?|contact|events?)\b", re.I)
SUB_NOISE = re.compile(
    r"(explore our|get educated|see how|learn more|read more|find out more|"
    r"discover our|watch the|sign up|subscribe|join us)", re.I)
DATE_PAT = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2})|(20\d{2}[/-]\d{1,2}[/-]\d{1,2})|"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2})", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_human_date(s):
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(20\d{2})", s)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return "%s-%02d-%02d" % (m.group(3), mo, int(m.group(2)))
    return ""


def fetch_site(name, url):
    try:
        text = get(url, timeout=25)
    except Exception as e:
        print("    %-22s 抓取失败: %s" % (name, str(e)[:44]))
        return []
    out = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if not re.search(r"(news|press|blog|release|announce|article|detail|通知|新闻|公告)", href, re.I):
            continue
        # 先剥掉页面上的栏目标签前缀，别把真新闻当导航丢掉
        title = re.sub(
            r"^(press release|news release|news|blog|announcement|media)\s*[:\-—|·]?\s*",
            "", clean(inner), flags=re.I).strip()
        title = re.sub(r"^\s*\d{4}-\d{2}-\d{2}\s*", "", title).strip()
        title = re.sub(r"^\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}\s*",
                       "", title, flags=re.I).strip(" |·—-")
        # 锚文本常把标题、摘要和「Learn more」按钮裹在一起，先切掉尾巴再判断
        sm = SUB_NOISE.search(title)
        if sm:
            title = title[:sm.start()].strip(" |·—-\t")
        if len(title) < 22 or len(title) > 200:
            continue
        if NAV_NOISE.match(title):
            continue
        if not QUANTUM_HINT.search(title) and name not in ("国盾量子",):
            # 官网新闻标题通常自带企业名或量子词，两者都没有则多半是导航
            if not re.search(re.escape(name.split()[0]), title, re.I):
                continue
        ctx = text[max(0, m.start() - 500):m.end() + 500]
        dm = DATE_PAT.search(ctx)
        full = urljoin(url, href)
        if not full.startswith("http"):
            continue
        out.append({"title": title, "link": full, "date": parse_human_date(dm.group(0)) if dm else "", "desc": ""})
    return out


# ---------------------------------------------------------------- 汇总
def normalize(raw_items, source, days, force_company=""):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    seen = set()
    for r in raw_items:
        title = (r.get("title") or "").strip()
        link = (r.get("link") or "").strip()
        if source == "Google News":
            # Google News 标题格式固定为「标题 - 媒体名」，剥掉尾巴
            title = re.sub(r"\s+-\s+[^-]{2,32}$", "", title).strip()
        title = title.strip(" |·—-")
        if not title or not link:
            continue
        if len(title) < 15:
            continue
        blob = title + " " + (r.get("desc") or "")
        if NOISE.search(blob):
            continue
        if not QUANTUM_HINT.search(blob) and source != "SEC EDGAR":
            continue
        d = to_iso(r.get("date", "")) or (r.get("date") or "")
        if d and d < cutoff:
            continue
        key = hashlib.md5((title[:80]).lower().encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        co = force_company or match_company(title, r.get("desc") or "")
        meta = COMPANIES.get(co)
        out.append({
            "id": key[:12],
            "company": co,
            "company_en": meta[0] if meta else "",
            "country": meta[3] if meta else "",
            "city": meta[4] if meta else "",
            "track": meta[2] if meta else "",
            "type": guess_type(title, r.get("desc") or ""),
            "date": d,
            "title_en": title[:220],
            "title_zh": "",
            "summary_en": (r.get("desc") or "")[:300],
            "summary_zh": "",
            "url": link[:600],
            "source": source,
            "tier": SOURCE_TIER.get(source, "media"),
            "form": r.get("form", ""),
            # Google News 的 description 是拼接片段，容易把不相邻的词凑成参数，
            # 所以媒体线索只从标题抽；正文补抽也只做一手发布（见 enrich）
            "specs": extract_specs(title, "" if source == "Google News" else (r.get("desc") or "")),
        })
    return out


def enrich(items, limit=90):
    """对一手发布抓详情页补抽技术参数。

    只读取页面并抽取数值，不保存正文 —— 页面里留下的仍然只有「数值 + 原文链接」。
    """
    targets = [i for i in items if i.get("tier") == "official" and i.get("url")][:limit]

    def one(it):
        try:
            raw = get(it["url"], timeout=15, retries=0)
        except Exception:
            return (None, None)
        t = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
        t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = html.unescape(re.sub(r"\s+", " ", t))
        return extract_specs(t[:8000], "", ctx=True)

    if not targets:
        return 0, 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(one, targets))
    added, touched = 0, 0
    for it, (sp, ctxs) in zip(targets, res):
        if not sp:
            continue
        hit = False
        for k, v in sp.items():
            if k not in it.get("specs", {}):
                it.setdefault("specs", {})[k] = v
                if ctxs and ctxs.get(k):
                    it.setdefault("spec_ctx", {})[k] = ctxs[k]
                added += 1
                hit = True
        if hit:
            touched += 1
    return added, touched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="回溯天数")
    ap.add_argument("--out", default=DATA + "/items.json")
    ap.add_argument("--no-enrich", action="store_true", help="跳过详情页补抽（更快）")
    args = ap.parse_args()

    print("=" * 72)
    print("Quantum Wire 采集器  %s  回溯 %d 天"
          % (datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z"), args.days))
    print("=" * 72)

    all_items = []

    print("\n[1/3] SEC EDGAR（核实层 · 上市公司申报）")
    tm = sec_ticker_map()
    print("  解析到 %d 家上市公司 CIK: %s" % (len(tm), ", ".join(tm.keys()) or "无"))
    sec_raw = sec_fetch(tm, days=max(args.days, 45))
    print("  拉到 %d 条申报" % len(sec_raw))
    all_items += normalize(sec_raw, "SEC EDGAR", max(args.days, 45))

    print("\n[2/3] Google News（发现层 · 行业线索）")
    gn_raw = google_news(days=args.days)
    print("  拉到 %d 条原始条目" % len(gn_raw))
    all_items += normalize(gn_raw, "Google News", args.days)

    print("\n[3/4] PR Newswire（核实层 · 稿件分发）")
    pr_raw = pr_newswire()
    print("  拉到 %d 条原始条目" % len(pr_raw))
    all_items += normalize(pr_raw, "PR Newswire", 2)

    print("\n[4/4] 企业官网 Newsroom（核实层 · 一手原文链接）")
    site_items = []

    def one_site(kv):
        name, url = kv
        r = fetch_site(name, url)
        print("    %-24s %d 条" % (name, len(r)))
        return normalize(r, "企业官网", args.days * 3, force_company=name)

    with ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(one_site, list(SITES.items())):
            site_items.extend(r)
    all_items += site_items

    # 去重：同 title 前缀 + 同公司
    seen, final = set(), []
    for it in sorted(all_items, key=lambda x: (x["date"], x["source"]), reverse=True):
        k = (it["company"], it["title_en"][:60].lower())
        if k in seen:
            continue
        seen.add(k)
        final.append(it)

    if not args.no_enrich:
        print("\n[补充] 一手发布详情页参数补抽（只读页面抽数值，不保存正文）")
        added, touched = enrich(final)
        print("  补抽 %d 个参数，覆盖 %d 条发布" % (added, touched))

    named = sum(1 for i in final if i["company"])
    print("\n" + "=" * 72)
    print("汇总：%d 条（已去重）  其中可识别企业 %d 条  未匹配 %.0f%%"
          % (len(final), named, (100 * (len(final) - named) / max(len(final), 1))))
    by_src = {}
    for i in final:
        by_src[i["source"]] = by_src.get(i["source"], 0) + 1
    for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
        print("  %-16s %d" % (k, v))
    by_co = {}
    for i in final:
        if i["company"]:
            by_co[i["company"]] = by_co.get(i["company"], 0) + 1
    print("  覆盖企业 %d 家" % len(by_co))
    for k, v in sorted(by_co.items(), key=lambda x: -x[1])[:12]:
        print("    %-26s %d" % (k, v))
    print("=" * 72)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "count": len(final),
        "items": final,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n已写入 %s（%d 条）" % (args.out, len(final)))

    off = sum(1 for i in final if i["tier"] == "official")
    print("  其中企业一手发布 %d 条，媒体线索 %d 条" % (off, len(final) - off))

    comp = {}
    for name, m in COMPANIES.items():
        comp[name] = {"abbr": m[0], "color": m[1], "track": m[2], "country": m[3], "city": m[4]}
    cpath = os.path.join(os.path.dirname(args.out), "companies.json")
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump({"countries": COUNTRIES, "companies": comp}, f, ensure_ascii=False, indent=1)
    print("已写入 %s（%d 家企业 / %d 个国家地区）" % (cpath, len(comp), len(COUNTRIES)))


if __name__ == "__main__":
    main()
