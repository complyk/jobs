#!/usr/bin/env python3
"""UAE-relevant employers to probe across the ATS registry.

Grouped by sector so the coverage gaps are visible. Slug variants are generated
automatically (lowercase, de-spaced, hyphenated, 'the'/'group' stripped), because
a company's ATS tenant name rarely matches its trading name exactly.
"""

import re

BANKS = """First Abu Dhabi Bank|FAB, Emirates NBD, Abu Dhabi Commercial Bank|ADCB,
Mashreq, Abu Dhabi Islamic Bank|ADIB, Dubai Islamic Bank|DIB, RAKBank,
Commercial Bank of Dubai|CBD, National Bank of Fujairah|NBF, Emirates Islamic,
Ajman Bank, Sharjah Islamic Bank, United Arab Bank, Invest Bank, Wio Bank,
Zand Bank, Liv Bank, Al Maryah Community Bank, HSBC, Standard Chartered, Citi,
Barclays, Deutsche Bank, JPMorgan, Goldman Sachs, Morgan Stanley, BNP Paribas,
Societe Generale, Credit Agricole, Julius Baer, UBS, Lombard Odier, Pictet,
EFG Hermes, Arqaam Capital, SHUAA Capital, Emirates Investment Bank"""

CRYPTO = """Binance, Bybit, OKX, Crypto.com, Kraken, Bitget, KuCoin, Gate.io,
MEXC, HTX, BingX, Deribit, Bitfinex, Coinbase, Circle, Ripple, Chainalysis,
Fireblocks, Paxos, Gemini, BitGo, Copper, Zodia Custody, Anchorage Digital,
Galaxy Digital, Wintermute, Amber Group, FalconX, Keyrock, Flowdesk, B2C2,
Hidden Road, BitOasis, Rain, Fuze, M2, Multibank, Laser Digital, Nomura,
Standard Custody, Komainu, Hex Trust, Ceffu, Tether, Sygnum, Backed, Securitize,
Ondo Finance, Aquanow, Crypto Finance, Bitpanda, Blockchain.com, eToro"""

FINTECH = """Stripe, Checkout.com, Adyen, Rapyd, Nium, Thunes, Airwallex, Wise,
Revolut, Payoneer, Remitly, Western Union, PayPal, Visa, Mastercard,
Network International, Magnati, Telr, Tap Payments, MamoPay, Ziina, Mamo,
Postpay, Tabby, Tamara, Spotii, Cashew, Huspy, Sarwa, Baraka, StashAway,
Lean Technologies, Tarabut Gateway, NymCard, Fintech Galaxy, HubPay, Denarii,
Alaan, Pemo, Qashio, Xpence, YAP, Careem Pay, e& money, Astra Tech, Botim,
Pyypl, Foloosi, Paymennt, Ottu, MyFatoorah, PayTabs, Amwal, Klarna, Ebury,
Deel, Papaya Global, Remote, Multiplier, Plum, Sable, Wamda"""

BROKERS = """Capital.com, XTB, Plus500, Exness, Swissquote, Saxo Bank,
Interactive Brokers, IG Group, LMAX, CMC Markets, Tickmill, Pepperstone,
AxiTrader, FxPro, ThinkMarkets, ADSS, Equiti, GTN, Century Financial,
Al Ramz, Daman Securities, International Securities, BHM Capital,
Menacorp, Al Dhabi Brokerage, Thndr, Baraka Financial, Sarwa Digital"""

ASSET_MGMT = """Mubadala, ADQ, ADIA, Lunate, Investcorp, Gulf Capital,
Waha Capital, Abu Dhabi Capital Group, Chimera Capital, Alpha Dhabi,
International Holding Company, Emirates Investment Authority, Dubai Holding,
Dubai Investments, ICD, DIFC Investments, Fajr Capital, Amanat Holdings,
Al Mal Capital, Daman Investments, Emirates NBD Asset Management, NBK Capital,
BlackRock, Franklin Templeton, Fidelity, Schroders, Amundi, PIMCO"""

INSURANCE = """Sukoon Insurance, Salama, GIG Gulf, Orient Insurance,
Emirates Insurance, Abu Dhabi National Insurance|ADNIC, Dubai Insurance,
Al Ain Ahlia, Watania, Takaful Emarat, Union Insurance, RAK Insurance,
AXA Gulf, Zurich, Allianz, Chubb, MetLife, Marsh, Aon, WTW, Gallagher"""

EXCHANGES_REG = """Dubai Financial Market|DFM, Abu Dhabi Securities Exchange|ADX,
Nasdaq Dubai, DIFC, ADGM, Dubai Multi Commodities Centre|DMCC, VARA,
Securities and Commodities Authority, Central Bank of the UAE, DFSA, Hub71,
Dubai Chamber, Dubai Economy, Abu Dhabi Global Market"""

ADVISORY = """Deloitte, PwC, EY, KPMG, Grant Thornton, BDO, Crowe, Mazars,
Forvis Mazars, RSM, Baker Tilly, Alvarez and Marsal, FTI Consulting, Kroll,
Control Risks, K2 Integrity, Accenture, McKinsey, Bain, Boston Consulting Group,
Oliver Wyman, Protiviti, Marsh McLennan, Aurexia, Cognizant, Capgemini"""

CORPORATES = """Emaar, DAMAC, Aldar, Majid Al Futtaim, Chalhoub Group,
Al Futtaim, Landmark Group, Alshaya, Emirates Group, Etihad Airways, flydubai,
Air Arabia, DP World, AD Ports, ADNOC, ENOC, Masdar, TAQA, Emirates Global Aluminium,
Etisalat, e& , du, G42, Presight, Core42, Space42, Careem, Noon, Talabat,
Deliveroo, Kitopi, Swvl, Property Finder, Bayut, Dubizzle, Tabby, Anghami,
Yalla Group, Alef Education, Astra Tech, Amazon, Google, Microsoft, Meta,
Oracle, SAP, Salesforce, IBM, Siemens"""

RECRUITERS = """Michael Page, Robert Walters, Hays, Morgan McKinley, Charterhouse,
Cooper Fitch, Robert Half, Mark Williams, Nadia Global, Selby Jennings, Huxley,
Taylor Root, Barclay Simpson, Jameson Legal, Halian, Tiger Recruitment,
Inspire Selection, Mayfair Partners, King Deux, AMT Partners, Korn Ferry,
Heidrick and Struggles, Spencer Stuart, Egon Zehnder, Russell Reynolds,
Alexander Hughes, Boyden, Stanton Chase, Odgers Berndtson, Page Executive,
BAC Middle East, Kershaw Leonard, Manpower, Adecco, Randstad, Hudson,
Gulf Connexions, Sapphire Recruitment, Black Pearl, Irwin and Dow"""

GROUPS = {
    "banks": BANKS, "crypto": CRYPTO, "fintech": FINTECH, "brokers": BROKERS,
    "asset_management": ASSET_MGMT, "insurance": INSURANCE,
    "exchanges_regulators": EXCHANGES_REG, "advisory": ADVISORY,
    "corporates": CORPORATES, "recruiters": RECRUITERS,
}

STOP = {"the", "group", "holding", "holdings", "company", "co", "llc", "plc",
        "pjsc", "limited", "ltd", "international", "and"}


def slug_variants(name):
    """Generate plausible ATS tenant slugs for a company name."""
    name = name.strip()
    base = re.sub(r"[^\w\s.-]", " ", name.lower())
    base = base.replace(".", "").strip()
    words = [w for w in re.split(r"[\s_-]+", base) if w]
    core = [w for w in words if w not in STOP] or words
    out = []

    def push(v):
        v = re.sub(r"[^a-z0-9-]", "", v)
        if v and 2 < len(v) < 40 and v not in out:
            out.append(v)

    push("".join(words))
    push("".join(core))
    push("-".join(core))
    if len(core) > 1:
        push(core[0])
        push("".join(w[0] for w in core))      # acronym
    push(core[0] + "group" if core else "")
    push("".join(core) + "careers")
    return out[:6]


def all_employers():
    """[(sector, display_name, [slug variants]), ...] with aliases expanded."""
    out = []
    for sector, blob in GROUPS.items():
        for raw in [x.strip() for x in blob.replace("\n", " ").split(",")]:
            if not raw:
                continue
            names = [n.strip() for n in raw.split("|") if n.strip()]
            display = names[0]
            variants = []
            for n in names:
                for v in slug_variants(n):
                    if v not in variants:
                        variants.append(v)
            out.append((sector, display, variants))
    return out


if __name__ == "__main__":
    emp = all_employers()
    print(f"{len(emp)} employers, {sum(len(v) for _, _, v in emp)} slug variants")
    for sector in GROUPS:
        n = sum(1 for s, _, _ in emp if s == sector)
        print(f"  {sector:22} {n}")
