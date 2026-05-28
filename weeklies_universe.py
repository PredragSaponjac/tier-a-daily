"""
OFFICIAL CBOE WEEKLY OPTIONS UNIVERSE
Source: https://www.cboe.com/available_weeklys
Last updated: 2026-03-02
Total: 671 unique tickers (103 ETFs + 568 equities)

This is the COMPLETE list of tickers with weekly options.
Update periodically from the CBOE link above.
"""

# ── ETFs & ETNs with weekly options ──
WEEKLIES_ETFS = [
    "AGQ","AMDL","ARKG","ARKK","ASHR","BITO","BITX","BOIL","CONL","COPX",
    "DIA","DPST","EEM","EFA","ETH","ETHA","ETHE","ETHU","EWJ","EWY",
    "EWZ","FAS","FBTC","FEZ","FXI","GBTC","GDX","GDXJ","GLD","HYG",
    "IAU","IBIT","ICLN","IEF","IGV","IVV","IWM","IYR","JETS","KOLD",
    "KRE","KWEB","LABD","LABU","LQD","MAGS","METU","MSOS","MSTU","MSTX",
    "MSTY","MSTZ","NAIL","NUGT","NVDL","NVDX","QQQ","RSP","SCHD","SILJ",
    "SLV","SMH","SOXL","SOXS","SOXX","SPXL","SPXS","SPXU","SPY","SQQQ",
    "SSO","SVIX","TLT","TMF","TNA","TQQQ","TSLL","TZA","ULTY","UNG",
    "UPRO","URA","USO","UVIX","UVXY","VOO","VXX","XBI","XHB","XLB",
    "XLC","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY","XOP",
    "XRT","YINN","ZSL",
]

# ── Equities with weekly options ──
WEEKLIES_EQUITIES = [
    "AA","AAL","AAOI","AAP","AAPL","ABAT","ABBV","ABNB","ABR","ABT",
    "ABTC","ABVX","ACHR","ACI","ACN","ADBE","ADI","ADP","ADSK","AEM",
    "AEO","AES","AFL","AFRM","AG","AGNC","AI","AIG","ALAB","ALB",
    "ALGN","ALT","AMAT","AMBA","AMC","AMD","AMGN","AMZN","ANET","ANF",
    "ANVS","APA","APLD","APO","APP","APT","AR","ARES","ARM","ASAN",
    "ASML","ASO","ASPI","ASST","ASTS","ATYR","AUR","AVAV","AVGO","AXP",
    "B","BA","BABA","BAC","BAX","BB","BBAI","BBWI","BBY","BE",
    "BEKE","BHC","BIDU","BIIB","BILI","BILL","BITF","BKKT","BKNG","BLK",
    "BLSH","BMNR","BMY","BP","BROS","BRR","BSX","BTBT","BTDR","BTG",
    "BTU","BUD","BULL","BURL","BX","BYND","C","CAG","CAH","CAR",
    "CART","CAT","CAVA","CBOE","CC","CCJ","CCL","CDE","CDNS","CEG",
    "CELH","CF","CGC","CHPT","CHTR","CHWY","CI","CIEN","CIFR","CL",
    "CLF","CLOV","CLS","CLSK","CMCSA","CME","CMG","CNC","CNQ","COF",
    "COHR","COIN","COP","CORZ","COST","CPB","CPNG","CRCL","CRDO","CRH",
    "CRM","CRML","CRSP","CRWD","CRWV","CSCO","CSIQ","CSX","CTAS","CTRA",
    "CVNA","CVS","CVX","CWAN","CZR","DAL","DASH","DBRG","DBX","DDD",
    "DDOG","DE","DECK","DELL","DFDV","DG","DHI","DHR","DIS","DJT",
    "DKNG","DLR","DLTR","DNN","DNUT","DOCN","DOCU","DOW","DUOL","DVN",
    "DXCM","EA","EBAY","EH","ELF","EMR","ENPH","ENVX","EOG","EOSE",
    "EPD","EQT","ET","ETN","ETSY","EXPE","F","FCEL","FCX","FDX",
    "FFAI","FHN","FIG","FIGR","FIS","FISV","FLY","FRMI","FSLR","FSLY",
    "FTAI","FTNT","FUBO","FUTU","GAP","GD","GDDY","GE","GEHC","GEMI",
    "GEV","GILD","GLW","GLXY","GM","GME","GNRC","GOOG","GOOGL","GOOS",
    "GPRO","GRAB","GRRR","GS","GSK","GTLB","GTM","HAL","HD","HIMS",
    "HIVE","HL","HLF","HLT","HOG","HON","HOOD","HPE","HPQ","HRL",
    "HSBC","HSY","HTZ","HUM","HUT","HWM","IBKR","IBM","IBRX","INFQ",
    "INO","INOD","INTC","INTU","IONQ","IOT","IOVA","IP","IQ","IREN",
    "IRM","ISRG","JBL","JBLU","JD","JMIA","JNJ","JOBY","JPM","KGC",
    "KHC","KKR","KLAR","KMB","KMI","KO","KOPN","KR","KSS","KTOS",
    "KVUE","LAC","LAES","LASE","LCID","LDI","LEN","LEVI","LHX","LI",
    "LITE","LLY","LMND","LMT","LNG","LOW","LQDA","LRCX","LULU","LUMN",
    "LUNR","LUV","LVS","LW","LYFT","M","MA","MANU","MAR","MARA",
    "MBLY","MCD","MCHP","MCK","MDB","MDLZ","MDT","MELI","META","MGM",
    "MMM","MNKD","MO","MP","MPT","MRK","MRNA","MRVL","MS","MSFT",
    "MSTR","MT","MU","MVIS","NAK","NB","NBIS","NCLH","NEE","NEM",
    "NET","NFE","NFLX","NIO","NKE","NLY","NMAX","NN","NNE","NOK",
    "NOW","NRG","NSC","NTR","NU","NVAX","NVDA","NVO","NVTS","OCUL",
    "OKLO","OKTA","ON","ONDS","ONON","OPAD","OPEN","ORCL","OSCR","OUST",
    "OWL","OXY","PAA","PAAS","PANW","PATH","PBR","PCG","PCT","PDD",
    "PEP","PFE","PG","PGR","PHM","PINS","PL","PLTR","PLUG","PM",
    "PNC","POET","PONY","PPG","PSKY","PSQH","PSX","PTON","PYPL","QBTS",
    "QCOM","QS","QSI","QUBT","QXO","RACE","RANI","RBLX","RBRK","RCAT",
    "RCL","RDDT","RDW","REGN","REPL","RGTI","RH","RIG","RILY","RIOT",
    "RIVN","RKLB","RKT","RNG","ROKU","ROST","RR","RTX","RUM","RUN",
    "RXRX","RXT","RZLV","S","SAP","SATS","SAVA","SBET","SBUX","SCCO",
    "SCHW","SE","SEDG","SERV","SG","SGML","SHAK","SHEL","SHOP","SIG",
    "SIRI","SKYT","SLB","SLS","SMCI","SMMT","SMR","SNAP","SNDK","SNOW",
    "SNPS","SO","SOC","SOFI","SONY","SOUN","SPCE","SPGI","SPOT","SRPT",
    "STLA","STUB","STX","STZ","SU","SYM","T","TDOC","TE","TEAM",
    "TECK","TEM","TER","TEVA","TGT","TIGR","TJX","TLN","TLRY","TMC",
    "TMO","TMQ","TMUS","TOST","TPR","TRIP","TSCO","TSLA","TSM","TSSI",
    "TTD","TTWO","TWLO","TXN","TXRH","U","UAL","UAMY","UBER","UEC",
    "ULTA","UMAC","UNH","UNP","UPS","UPST","UPXI","URBN","URI","USAR",
    "USB","UUUU","UWMC","V","VALE","VERU","VFC","VG","VKTX","VLO",
    "VOD","VRT","VRTX","VST","VZ","W","WBD","WDAY","WDC","WEN",
    "WFC","WGS","WMB","WMT","WOOF","WPM","WRBY","WULF","WWR","WYNN",
    "XOM","XP","XPEV","XYZ","ZETA","ZIM","ZM","ZS",
]

def get_all_tickers():
    """Get complete deduplicated list of all weekly options tickers."""
    seen = set()
    tickers = []
    for t in WEEKLIES_ETFS + WEEKLIES_EQUITIES:
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    return tickers

if __name__ == "__main__":
    all_t = get_all_tickers()
    print(f"ETFs: {len(WEEKLIES_ETFS)}")
    print(f"Equities: {len(WEEKLIES_EQUITIES)}")
    print(f"Total unique: {len(all_t)}")
