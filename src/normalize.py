"""ingredient name normalization + atc helpers.

regex-heavy on purpose: this is chemistry naming, not generic text cleanup.
one place so fda / hsa / orphan matching all share the same vocab.
"""

from __future__ import annotations

import math
import re
import string
from collections.abc import Iterable
from typing import TypeVar


# salts / esters / hydrates / counterions. strip these so one molecule doesn't
# split across countries just because the label picked a different salt.
SALT_STRIP_TERMS = (
    "hydrochloride",
    "hcl",
    "hydrobromide",
    "hbr",
    "hydroiodide",
    "bromide",
    "chloride",
    "iodide",
    "fluoride",
    "sodium",
    "potassium",
    "calcium",
    "magnesium",
    "lithium",
    "zinc",
    "aluminum",
    "sulfate",
    "sulphate",
    "phosphate",
    "nitrate",
    "carbonate",
    "bicarbonate",
    "acetate",
    "benzoate",
    "citrate",
    "fumarate",
    "maleate",
    "malate",
    "mesylate",
    "besylate",
    "tartrate",
    "tosylate",
    "oxalate",
    "pamoate",
    "succinate",
    "gluconate",
    "lactate",
    "aspartate",
    "glutamate",
    "edisylate",
    "napsylate",
    "embonate",
    "isethionate",
    "camsylate",
    "stearate",
    "valerate",
    "propionate",
    "caproate",
    "decanoate",
    "enanthate",
    "cypionate",
    "undecanoate",
    "palmitate",
    "dipropionate",
    "acetonide",
    "furoate",
    "monohydrate",
    "dihydrate",
    "trihydrate",
    "trihydate",
    "tetrahydrate",
    "pentahydrate",
    "hydrate",
    "anhydrous",
    "trometamol",
    "tromethamine",
    "monoacetate",
    "base",
    "heptahydrate",
    "hexahydrate",
    "bitartrate",
    "subcitrate",
    "subsalicylate",
    "tebutate",
    "dibasic",
    "monobasic",
    "glacial",
    "micronized",
    "bisulfate",
    "bisulphate",
    "hemifumarate",
    "hemihydrate",
    "hemisulfate",
    "xinafoate",
    "olamine",
    "saccharate",
    "nicotinate",
    "propylene",
    "dihcl",
    "dihbr",
    "dihydrochloride",
    "dihydrobromide",
    "trihydrochloride",
    "tetrahydrochloride",
    "sesquihydrate",
    # di/tri-prefix salts + ester variants
    "ditosylate",
    "tosilate",
    "trisodium",
    # ethylsuccinate is an ester salt. note we don't strip "mepesuccinate",
    # it's part of omacetaxine's inn.
    "ethylsuccinate",
    "butylbromide",
    "hydrogenbromide",
    "mononitrate",
    "polistirex",
    "epolamine",
    "diethylamine",
    "diethylammonium",
    "meglumine",
    "dimeglumine",
    "tromethamol",
    "ethanesulfonate",
    "methanesulfonate",
    "benzenesulfonate",
    "methylsulfate",
    "methylsulphate",
    # di-cation salts + plural mesylate
    "dimaleate",
    "diphosphate",
    "diacetate",
    "disodium",
    "dipotassium",
    "dimesylate",
    "mesylates",
    "ditromethamine",
    "diacid",
    "hemipentahydrate",
    "trifenatate",
    "fumaric",
    "free",
    # dmso shows up as an excipient on some hsa labels
    "sulfoxide",
)


# dose form / route / pharmacopoeia / excipient noise. mostly hsa-side, but
# harmless on fda strings.
DOSE_FORM_TERMS = (
    "tablet",
    "tablets",
    "capsule",
    "capsules",
    "syrup",
    "suspension",
    "injection",
    "infusion",
    "cream",
    "ointment",
    "gel",
    "drops",
    "drop",
    "paste",
    "lozenge",
    "lozenges",
    "powder",
    "solution",
    "suppository",
    "suppositories",
    "spray",
    "inhaler",
    "patch",
    "lotion",
    "emulsion",
    "granules",
    "elixir",
    "implant",
    "pellets",
    "pellet",
    "sachets",
    "sachet",
    "enema",
    "aerosol",
    "nebuliser",
    "nebulizer",
    "liquid",
    "wafer",
    "gargle",
    "mouthwash",
    "shampoo",
    "wash",
    "foam",
    "plaster",
    "film",
    "coated",
    "release",
    "extended",
    "modified",
    "sustained",
    "controlled",
    "prolonged",
    "delayed",
    "immediate",
    "enteric",
    "gastro",
    "resistant",
    "sugar",
    "chewable",
    "effervescent",
    "oral",
    "topical",
    "sublingual",
    "buccal",
    "ophthalmic",
    "otic",
    "dermal",
    "nasal",
    "inhalation",
    "rectal",
    "vaginal",
    "intramuscular",
    "intravenous",
    "subcutaneous",
    "ip",
    "bp",
    "usp",
    "ep",
    "jp",
    "nf",
    "human",
    "veterinary",
    "paediatric",
    "pediatric",
    # grade / excipient descriptors
    "hypromellose",
    "lactose",
    "absolute",
    "milled",
    "microcrystalline",
    "micronised",
    "cellulose",
    "diluted",
    "silica",
    "colloidal",
    "hydrated",
    "sterile",
    "dried",
    "powdered",
    "light",
    "heavy",
    "buffered",
    "hydrous",
    "betadex",
    "clathrate",
    "ultramicrosize",
    "chimeric",
    "adsorbed",
    "elemental",
    "phenol",
    "precipitate",
    "hyd",
    "vial",
    "hydrous",
    # insulin: fda writes "recombinant", hsa writes "rdna"
    "recombinant",
    "rdna",
    # stabilizer/excipient labels seen inside hsa active strings
    "bht",
    "bhq",
    "bha",
)


# us/uk/inn spelling + vitamin + typo fixes. order matters: applied before
# salt stripping so protected inns like dimethyl fumarate survive.
SYNONYM_REPLACEMENTS = (
    (r"\bacetaminophen\b", "paracetamol"),
    (r"\balbuterol\b", "salbutamol"),
    (r"\bepinephrine\b", "adrenaline"),
    (r"\bnorepinephrine\b", "noradrenaline"),
    (r"\bglyburide\b", "glibenclamide"),
    (r"\bmeperidine\b", "pethidine"),
    (r"\bfurosemide\b", "frusemide"),
    (r"\bacyclovir\b", "aciclovir"),
    (r"\bvalacyclovir\b", "valaciclovir"),
    (r"\baspirin\b", "acetylsalicylic acid"),
    (r"\bcephalexin\b", "cefalexin"),
    (r"\bcephradine\b", "cefradine"),
    (r"\brifampin\b", "rifampicin"),
    (r"\bchlorpheniramine\b", "chlorphenamine"),
    (r"\bbeclomethasone\b", "beclometasone"),
    (r"\bcyclosporin\b", "ciclosporin"),
    (r"\bchlorthalidone\b", "chlortalidone"),
    (r"\bsulphamethoxazole\b", "sulfamethoxazole"),
    (r"\boestradiol\b", "estradiol"),
    (r"\boestrogen\b", "estrogen"),
    (r"\blignocaine\b", "lidocaine"),
    (r"\baluminium\b", "aluminum"),
    (r"\bscopolamine\b", "hyoscine"),
    (r"\bdibucaine\b", "cinchocaine"),
    (r"\bleuprolide\b", "leuprorelin"),
    (r"\bmesalamine\b", "mesalazine"),
    # usan / older names where the who atc l5 name is spelled differently
    (r"\bcyclosporine\b", "ciclosporin"),
    (r"\bnorethindrone\b", "norethisterone"),
    (r"\bnitroglycerin\b", "glyceryl trinitrate"),
    (r"\bglyceryl\s+trinitrate\s*\(?\s*gtn\b.*\bglucose\b", "glyceryl trinitrate"),
    (r"\bglycopyrrolate\b", "glycopyrronium"),
    (r"\bisoproterenol\b", "isoprenaline"),
    (r"\bmetaproterenol\b", "orciprenaline"),
    (r"\bsuccinylcholine\b", "suxamethonium"),
    (r"\bcromolyn\b", "cromoglicic acid"),
    (r"\bcromoglycate\b", "cromoglicic acid"),
    (r"\bphytonadione\b", "phytomenadione"),
    (r"\bcalcipotriene\b", "calcipotriol"),
    (r"\btorsemide\b", "torasemide"),
    (r"\bdicyclomine\b", "dicycloverine"),
    (r"\bdivalproex\b", "valproic acid"),
    (r"\bvalproate\b", "valproic acid"),
    (r"\bhydroxyurea\b", "hydroxycarbamide"),
    (r"\bbenztropine\b", "benzatropine"),
    (r"\bdiethylpropion\b", "amfepramone"),
    (r"\bdextroamphetamine\s+resin\s+complex\b", "dexamfetamine"),
    (r"\bamphetamine\s+resin\s+complex\b", "amfetamine"),
    (r"\bdextroamphetamine\b", "dexamfetamine"),
    (r"\bamphetamine\b", "amfetamine"),
    (r"\bmethamphetamine\b", "metamfetamine"),
    (r"\bchenodiol\b", "chenodeoxycholic acid"),
    (r"\bmethylergonovine\b", "methylergometrine"),
    (r"\bmeclizine\b", "meclozine"),
    (r"\bcholestyramine\s+resin\b", "colestyramine"),
    (r"\bcholestyramine\b", "colestyramine"),
    (r"\bclomiphene\b", "clomifene"),
    (r"\bmethimazole\b", "thiamazole"),
    (r"\betidronate\b", "etidronic acid"),
    (r"\bursodiol\b", "ursodeoxycholic acid"),
    (r"\bdicumarol\b", "dicoumarol"),
    (r"\bflurandrenolide\b", "fludroxycortide"),
    (r"\bthiothixene\b", "tiotixene"),
    (r"\bezogabine\b", "retigabine"),
    (r"\bpropoxyphene\b", "dextropropoxyphene"),
    (r"\blevoleucovorin\b", "levofolinate"),
    (r"\bgonadotropin\s*,?\s+chorionic\b", "chorionic gonadotrophin"),
    (r"\bchorionic\s+gonadotropin\b", "chorionic gonadotrophin"),
    (r"\bmenotropins?\s*\(?fsh\b", "menopausal gonadotrophin"),
    (r"\bmenotrophin\s+highly\s+purified\b", "menopausal gonadotrophin"),
    (r"\bmenotropins?\b", "menopausal gonadotrophin"),
    (r"\btetrahydrozoline\b", "tetryzoline"),
    (r"\bproparacaine\b", "proxymetacaine"),
    (r"\bbenoxinate\b", "oxybuprocaine"),
    (r"\bcephapirin\b", "cefapirin"),
    (r"\bcephalothin\b", "cefalotin"),
    (r"\bamdinocillin\b", "mecillinam"),
    (r"\bmoxalactam\b", "latamoxef"),
    (r"\bmethsuximide\b", "mesuximide"),
    (r"\bniacinamide\b", "nicotinamide"),
    (r"\bniacin\b", "nicotinic acid"),
    (r"\bsodium\s+ascorbate\b", "ascorbic acid"),
    (r"\balpha[-\s]+tocopherol\b", "tocopherol"),
    (r"\bpolyethylene\s+glycol\b", "macrogol"),
    (r"\bazilsartan\s+kamedoxomil\b", "azilsartan medoxomil"),
    (r"\bn-?acetylcysteine\b", "acetylcysteine"),
    (r"\bisavuconazonium\b", "isavuconazole"),
    (r"\bcysteamine\b", "mercaptamine"),
    (r"\bisoetharine\b", "isoetarine"),
    (r"\brauwolfia\s+serpentina\s+root\b", "rauwolfia alkaloids whole root"),
    (r"\bcamphor\b", "camphora"),
    (r"\bprecipitated\s+sulphur\b", "sulfur"),
    (r"\bprecipitated\s+sulfur\b", "sulfur"),
    (r"^\s*dehydrated\s+alcohol\b", "ethanol"),
    (r"^\s*alcohol\s+absolute\b", "ethanol"),
    (r"^\s*alcohol\b", "ethanol"),
    (r"\boxidronate\b", "oxidronic acid"),
    (r"\bruxolinitib\b", "ruxolitinib"),
    (r"\bparoxetin\b", "paroxetine"),
    (r"\bgluclose\b", "glucose"),
    (r"\bestrogens?\s*,?\s*\(?\s*conjugated\)?\b", "conjugated estrogens"),
    (r"\boestrogens?\s*,?\s*\(?\s*conjugated\)?\b", "conjugated estrogens"),
    (r"\bdiatrizoate\b", "diatrizoic acid"),
    (r"\bamidotrizoate\b", "diatrizoic acid"),
    (r"\biothalamate\b", "iotalamic acid"),
    (r"\bmetrizoate\b", "metrizoic acid"),
    (r"\bgadobenate\b", "gadobenic acid"),
    (r"\bgadoterate\b", "gadoteric acid"),
    (r"\bgadopentetate\b", "gadopentetic acid"),
    (r"\bgadoxetate\b", "gadoxetic acid"),
    (r"\bioxaglate\b", "ioxaglic acid"),
    (r"\bbutabarbital\b", "butobarbital"),
    (r"\bsulfamethazine\b", "sulfadimidine"),
    (r"\bsulfisoxazole\b", "sulfafurazole"),
    # vitamin names -> inn
    (r"\bvitamin\s*a\b", "retinol"),
    (r"\bvitamin\s*b\s*1\b", "thiamine"),
    (r"\bvitamin\s*b\s*2\b", "riboflavin"),
    (r"\bvitamin\s*b\s*3\b", "nicotinamide"),
    (r"\bvitamin\s*b\s*5\b", "pantothenic acid"),
    (r"\bvitamin\s*b\s*6\b", "pyridoxine"),
    (r"\bvitamin\s*b\s*9\b", "folic acid"),
    (r"\bvitamin\s*b\s*12\b", "cyanocobalamin"),
    (r"\bvitamin\s*c\b", "ascorbic acid"),
    (r"\bvitamin\s*d\s*3\b", "colecalciferol"),
    (r"\bvitamin\s*d\s*2\b", "ergocalciferol"),
    (r"\bvitamin\s*d\b", "colecalciferol"),
    (r"\bvitamin\s*e\b", "tocopherol"),
    (r"\bvitamin\s*k\s*1\b", "phytomenadione"),
    (r"\bvitamin\s*k\b", "phytomenadione"),
    (r"\balpha\s+tocopherol\b", "tocopherol"),
    # free-acid / salt name variants
    (r"\balendronate\s+acid\b", "alendronate"),
    (r"\balendronic\s+acid\b", "alendronate"),
    (r"\brisedronic\s+acid\b", "risedronate"),
    (r"\bpamidronic\s+acid\b", "pamidronate"),
    (r"\bibandronic\s+acid\b", "ibandronate"),
    (r"\bfolinic\s+acid\b", "leucovorin"),
    (r"\bfolinate\b", "leucovorin"),
    (r"\bmycophenolate(?!\s+mofetil)\b", "mycophenolic acid"),
    (r"\bfusidate\b", "fusidic acid"),
    (r"\bcarbonyldiamide\b", "urea"),
    (r"\bcarbamide\b", "urea"),
    # spelling / naming variants
    (r"\bsimethicone\b", "simeticone"),
    (r"\bethinyl\s+estradiol\b", "ethinylestradiol"),
    (r"\bethinyloestradiol\b", "ethinylestradiol"),
    (r"\bdextrose\b", "glucose"),
    (r"\b(?:carbon|charcoal)\s+activated\b", "activated charcoal"),
    (r"\bactivated\s+carbon\b", "activated charcoal"),
    # uk / older inn spellings
    (r"\bphenobarbitone\b", "phenobarbital"),
    (r"\bamylobarbitone\b", "amobarbital"),
    (r"\bquinalbarbitone\b", "secobarbital"),
    (r"\bsulphadiazine\b", "sulfadiazine"),
    (r"\bsulphasalazine\b", "sulfasalazine"),
    (r"\bsulphur\b", "sulfur"),
    (r"\baminosidine\b", "paromomycin"),
    (r"\bindomethacin\b", "indometacin"),
    (r"\bdimethicone\b", "dimeticone"),
    (r"\bmethicillin\b", "meticillin"),
    (r"\bcholecalciferol\b", "colecalciferol"),
    (r"\bnaphthazoline\b", "naphazoline"),
    (r"\bspiramycine\b", "spiramycin"),
    (r"\bbenzyl\s+penicillin\b", "benzylpenicillin"),
    (r"\bphenoxymethyl\s+penicillin\b", "phenoxymethylpenicillin"),
    (r"\bpenicillin\s*v\b", "phenoxymethylpenicillin"),
    (r"\bpenicillin\s*g\b", "benzylpenicillin"),
    (r"\bbenzhexol\b", "trihexyphenidyl"),
    (r"\bpantothenate\b", "pantothenic acid"),
    (r"\bcoal\s+tar\s+prepared\b", "coal tar"),
    # ocr / typos
    (r"\bhci\b", "hcl"),
    (r"\bhbri\b", "hbr"),
    (r"\bchlorhidate\b", "chlorhydrate"),
    # fuse these so salt stripping can't split them, restored further down
    (r"\bmeglumine\s+antimonate\b", "meglumineantimonate"),
    (r"\bdimethyl\s+fumarate\b", "dimethylfumarate"),
    # hsa spelling variant
    (r"\bguaiphenesin\b", "guaifenesin"),
    # aaga salt label = spaglumic acid
    (r"\bacetyl\s+aspartyl\s+glutamic\s+acid(\s+salt)?\b", "spaglumic acid"),
    # moiety aliases for combo-only salt names
    (r"\bclavulanate\b", "clavulanic acid"),
    (r"\bamoxycillin\b", "amoxicillin"),
    # fda biologic name prefixes, not part of the stem
    (r"\bado-?trastuzumab\b", "trastuzumab"),
    (r"\bfam-?trastuzumab\b", "trastuzumab"),
)


# who 2021 atc reorg remap. applied to both fda matches and hsa codes before
# grouping by substance.
ATC_REMAP = {
    "L01XE01": "L01EA01",
    "L01XE02": "L01EB01",
    "L01XE04": "L01EX01",
    "L01XE05": "L01EX02",
    "L01XE06": "L01EA02",
    "L01XE08": "L01EA03",
    "L01XE10": "L01EG02",
    "L01XE11": "L01EX03",
    "L01XE13": "L01EB03",
    "L01XE15": "L01EC01",
    "L01XE16": "L01ED01",
    "L01XE17": "L01EK01",
    "L01XE21": "L01EX05",
    "L01XE31": "L01EX09",
    "L01XE33": "L01EF01",
    "L01XE35": "L01EB04",
    "L01XE36": "L01ED03",
    "L01XE43": "L01ED04",
    "L01XE44": "L01ED05",
    "L01XE47": "L01EB07",
    "L01XE56": "L01EX14",
    "L01EX17": "L01EP01",
    "L01EX21": "L01EP02",
    "L01XC02": "L01FA01",
    "L01XC03": "L01FD01",
    "L01XC05": "L01FX02",
    "L01XC08": "L01FE02",
    "L01XC11": "L01FX04",
    "L01XC12": "L01FX05",
    "L01XC14": "L01FD03",
    "L01XC17": "L01FF01",
    "L01XC18": "L01FF02",
    "L01XC19": "L01FX07",
    "L01XC21": "L01FG02",
    "L01XC24": "L01FC01",
    "L01XC28": "L01FF03",
    "L01XC31": "L01FF04",
    "L01XC38": "L01FC02",
    "L01XY03": "L01FY02",
    "L01XX14": "L01XF01",
    "L01XX19": "L01CE02",
    "L01XX32": "L01XG01",
    "L01XX50": "L01XG03",
    "L01XX60": "L01XK04",
    "L04AA11": "L04AB01",
    "L04AA13": "L04AK01",
    "L04AA18": "L04AH02",
    "L04AA27": "L04AE01",
    "L04AA29": "L04AF01",
    "L04AA33": "L04AG05",
    "L04AA34": "L04AG06",
    "L04AA37": "L04AF02",
    "L04AA42": "L04AE03",
    "L04AA44": "L04AF03",
    "N07XX09": "L04AX07",
    "N03AX12": "N02BF01",
    "N03AX16": "N02BF02",
    "N02CX07": "N02CD01",
    "N02CX08": "N02CD02",
    "N02AX52": "N02AJ13",
    # b03ac01 left unmapped: no valid l5 for ferric carboxymaltose in who 2026
    "B01AX06": "B01AF01",
    "J06BB16": "J06BD01",
    "R03AK03": "R03AL01",
    "J05AF30": "J05AR02",
    "J05AX08": "J05AJ01",
    "J05AX12": "J05AJ03",
    "A03AE04": "A06AX05",
    "A10BX07": "A10BJ02",
    "A10BX12": "A10BK03",
    "S01AX13": "S01AE03",
    "S01AX19": "S01AE05",
    "S01AX21": "S01AE06",
    "S01AX22": "S01AE07",
    "A01BA02": "A10BA02",
    "A20BB09": "A10BB09",
}


_SALT_STRIP_RE = re.compile(r"\b(" + "|".join(map(re.escape, SALT_STRIP_TERMS)) + r")\b", re.I)
_DOSE_FORM_RE = re.compile(r"\b(" + "|".join(map(re.escape, DOSE_FORM_TERMS)) + r")\b", re.I)
_MULTI_WORD_FORM_RE = re.compile(
    r"\b("
    r"dimethyl sulfoxide|fumaric acid|maleic acid|free acid|free base|"
    r"hydrogen sulfate|hydrogen sulphate|hydrogen tartrate|hydrogen fumarate"
    r")\b",
    re.I,
)
_PUNCT_TRANSLATION = str.maketrans({char: " " for char in string.punctuation})
_EQUIVALENCE_TARGET_RE = re.compile(
    r"\b("
    r"eq\.?\s*to|eqvivalent|eqvt\.?|eqv\.?|equv\.?|equiv(?:alent)?|"
    r"equivalent|equ\.?\s*to|correspond(?:s|ing)?\s+to"
    r")\s*(?:to\s+)?(.+)$",
    re.I,
)
_ISOTOPE_RE = re.compile(
    r"\b(tc|i|f|ga|cu|lu|y|in|sm|tl|c|n|o|rb|zr|zn|ra|sr|p|cr|fe)\s*-?\s*(\d{1,3}m?)(?!\s*%)\b",
    re.I,
)
_REVERSE_ISOTOPE_RE = re.compile(
    r"\b(\d{1,3}m?)(?!\s*%)\s*-?\s*(tc|i|f|ga|cu|lu|y|in|sm|tl|c|n|o|rb|zr|zn|ra|sr|p|cr|fe)\b",
    re.I,
)
_INORGANIC_ACTIVE_RE = re.compile(
    r"\b("
    r"aluminum hydroxide|aluminium hydroxide|magnesium hydroxide|"
    r"magnesium trisilicate|magnesium oxide|"
    r"sodium chloride|potassium chloride|calcium chloride|magnesium chloride|"
    r"sodium hydroxide|potassium hydroxide|"
    r"sodium fluoride|potassium fluoride|sodium iodide|potassium iodide|"
    r"sodium hydrogen carbonate|sodium dihydrogen phosphate|potassium dihydrogen phosphate|"
    r"calcium carbonate|magnesium carbonate|sodium bicarbonate|sodium carbonate|"
    r"magnesium sulfate|magnesium sulphate|sodium sulfate|sodium sulphate|"
    r"barium sulfate|barium sulphate|"
    r"potassium phosphate|sodium phosphate|calcium phosphate|"
    r"sodium acetate|potassium acetate|magnesium acetate|"
    r"sodium lactate|sodium gluconate"
    r")\b",
    re.I,
)
_BIOSIMILAR_SUFFIX_RE = re.compile(
    r"\b(?P<base>(?:insulin\s+)?[a-z]+(?:\s+(?:alfa|beta))?)\s*-\s*(?P<suffix>[a-z]{4})\b",
    re.I,
)
_BIOSIMILAR_BASE_STEMS = (
    "ase",
    "cept",
    "dotin",
    "feron",
    "germin",
    "grastim",
    "mab",
    "pegol",
    "plermin",
    "poetin",
    "tecan",
    "tide",
    "tox",
    "toxina",
    "toxinb",
    "tropin",
)
_BIOSIMILAR_BASE_TOKENS = {
    "chrysanthemi",
    "histolyticum",
}
_BIOSIMILAR_SUFFIX_BASE_ALLOWLIST = {
    "carbidopa",
    "careldopa",
    "dopa",
    "levo",
    "levodopa",
}
ATC_MATCH_ALIASES = {
    # atc file has b03xa01 as broad "erythropoietin". use it for atc metadata
    # only, identity stays epoetin alfa/beta so the qualifier isn't erased.
    "epoetin alfa": ("erythropoietin",),
    "epoetin beta": ("erythropoietin",),
    "activated charcoal": ("medicinal charcoal",),
    "factor xiii": ("coagulation factor xiii",),
    "factor ix": ("coagulation factor ix",),
    "factor viii": ("coagulation factor viii",),
    "antihemophilic factor": ("coagulation factor viii",),
    "antihemophilic factor viii": ("coagulation factor viii",),
    "blood coagulation factor viii": ("coagulation factor viii",),
    "factor vii": ("coagulation factor vii",),
    "factor x": ("coagulation factor x",),
    "fosaprepitant": ("aprepitant",),
    "mycophenolate mofetil": ("mycophenolic acid",),
    "methisoprinol": ("inosine pranobex",),
    "l lysine": ("lysine",),
    "l methionine": ("methionine",),
    "l tryptophan": ("tryptophan",),
}


def normalize_ingredient(value: object) -> str:
    """normalize one ingredient string into a comparison key."""

    text = "" if value is None else str(value)
    if _is_nan(value):
        text = ""
    original_text = text

    if _ISOTOPE_RE.search(text) or _REVERSE_ISOTOPE_RE.search(text):
        return _normalize_radiopharma(text)

    # greek letters as escapes so this stays encoding-stable
    text = text.replace("\u03b1", "alpha ")
    text = text.replace("\u03b2", "beta ")
    text = text.replace("\u03b3", "gamma ")
    text = text.replace("\u03b4", "delta ")
    text = re.sub("\u03bc|\u00b5", "u", text)
    text = text.lower()

    # split letter/digit boundaries so the \b salt and unit rules can match
    text = re.sub(r"(?<=[a-z])(\d)", r" \1", text)
    text = re.sub(r"(?<=\d)([a-z])", r" \1", text)
    text = re.sub(r"\ba-(?=[a-z]{4,})", "alpha-", text)
    text = re.sub(r"\bb-(?=[a-z]{4,})", "beta-", text)
    text = _prefer_equivalence_target(text)

    for pattern, replacement in SYNONYM_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)

    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"\bor\b", " ", text)
    text = re.sub(r"\beq\.?\s*to\b", " ", text)
    text = re.sub(r"\bequ\.?(\s+to)?\b", " ", text)
    text = re.sub(r"\b(eqvivalent|eqvt|eqv|equv)\.?(\s*to)?\b", " ", text)
    text = re.sub(r"\bequiv(alent)?(\s+to)?\b", " ", text)
    text = re.sub(r"\bcorrespond(s|ing)?\s+to\b", " ", text)
    text = re.sub(r"\bas\b", " ", text)
    text = re.sub(r"\bfor\b", " ", text)
    text = re.sub(r"\bin\b", " ", text)
    text = re.sub(r"\b(with|to|of)\b", " ", text)
    text = re.sub(
        r"\b\d+(\.\d+)?\s*(mg|mcg|\u00b5g|ug|g|ml|l|%|w/v|v/v|w/w|iu|units?|mol)(\s*/\s*\w+)?\b",
        " ",
        text,
    )
    text = re.sub(r"\b(mg|mcg|\u00b5g|ug|ml|iu|units?|mol|dose)\b", " ", text)
    text = _strip_biosimilar_suffixes(text)
    text = re.sub(r"\bbesilate\b", "besylate", text)
    text = re.sub(r"\bmesilate\b", "mesylate", text)
    text = re.sub(r"\bsulphate\b", "sulfate", text)
    text = re.sub(r"\bsulphide\b", "sulfide", text)

    # multi-word salt phrases before single-word ones, else we half-erase an inn
    text = _MULTI_WORD_FORM_RE.sub(" ", text)
    text = _SALT_STRIP_RE.sub(" ", text)
    text = _DOSE_FORM_RE.sub(" ", text)
    text = re.sub(r"\bmeglumineantimonate\b", "meglumine antimonate", text)
    text = re.sub(r"\bdimethylfumarate\b", "dimethyl fumarate", text)
    text = re.sub(r"\b(io\w+)\s+iodine\b", r"\1", text)
    text = text.translate(_PUNCT_TRANSLATION)
    text = re.sub(r"\d+", " ", text)
    text = _squish(text)

    # collapse "drug drug" -> "drug", keeping first-seen order
    tokens = text.split()
    unique_tokens: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique_tokens.append(token)
    inorganic = _normalize_inorganic_active(original_text)
    normalized = " ".join(unique_tokens)
    if inorganic and (
        normalized
        in {
            "",
            "hydrogen",
            "dihydrogen",
            "hydroxide",
            "light",
            "heavy",
            "oxide",
            "trisilicate",
            "barium",
        }
        or re.search(r"\b(?:incl|with|without)\b.*\b(?:combinations?|agents?|salts?)\b", original_text, re.I)
    ):
        return inorganic
    if normalized:
        return normalized

    if inorganic:
        return inorganic

    return ""


def normalize_fda_component(value: object, product_name: object = "") -> str:
    """normalize an fda component, fixing a couple product-context artifacts."""

    normalized = normalize_ingredient(value)
    if normalized != "follitropin alfa beta":
        return normalized

    product_text = "" if product_name is None else str(product_name).lower()
    if "gonal-f" in product_text:
        return "follitropin alfa"
    if "follistim" in product_text:
        return "follitropin beta"
    return normalized


def atc_match_names(component_norm: object) -> tuple[str, ...]:
    """atc lookup keys for a component, without changing its identity."""

    normalized = normalize_ingredient(component_norm)
    candidates = [normalized]
    candidates.extend(ATC_MATCH_ALIASES.get(normalized, ()))
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


T = TypeVar("T")


def fix_atc(value: T) -> T | str | None | list[str | None]:
    """fix hsa atc o->0 and reversed class-code typos."""

    if isinstance(value, str) or value is None or _is_nan(value):
        return _fix_one_atc(value)
    if isinstance(value, Iterable):
        return [_fix_one_atc(item) for item in value]
    return _fix_one_atc(value)


def split_fda_ingredients(value: object) -> list[str]:
    """split fda active strings on ; or AND."""

    if value is None or _is_nan(value):
        return []
    text = str(value)

    known_semicolon_literals = {
        "LIOTRIX (T4;T3)",
    }
    if text.upper().strip() in known_semicolon_literals:
        return [_squish(text)]

    known_space_delimited = {
        "DOLUTEGRAVIR LAMIVUDINE TENOFOVIR ALAFENAMIDE": [
            "DOLUTEGRAVIR",
            "LAMIVUDINE",
            "TENOFOVIR ALAFENAMIDE",
        ],
    }
    if text.upper().strip() in known_space_delimited:
        return known_space_delimited[text.upper().strip()]

    if ";" in text:
        return _split_and_squish(text, r";\s*|\s+AND\s+")
    if "," in text and re.search(r"\band\b", text, re.I):
        return _split_and_squish(text, r",\s*|\s+and\s+")
    return _split_and_squish(text, r"\s+AND\s+")


def split_hsa_ingredients(value: object) -> list[str]:
    """split hsa active strings on &&."""

    if value is None or _is_nan(value):
        return []
    text = str(value)
    if "&&" in text:
        return _split_and_squish(text, r"&&")

    lowered = text.lower()
    if "sacubitril/valsartan" in lowered:
        return ["sacubitril", "valsartan"]

    return _split_and_squish(text, r"&&")


def remap_atc_code(value: object) -> str | None:
    """apply the who 2021 remap to a cleaned atc code."""

    if value is None or _is_nan(value):
        return None
    text = str(value).strip().upper()
    return ATC_REMAP.get(text, text)


def _fix_one_atc(value: object) -> str | None:
    if value is None or _is_nan(value):
        return None

    text = str(value).strip().upper()
    if re.search(r"^pending$", text, re.I):
        return None

    if len(text) == 7:
        text = re.sub(r"^([A-Z])([A-Z])(\d)([A-Z]{2}\d{2})$", r"\g<1>0\3\4", text)
        chars = list(text)
        for pos in (1, 2, 5, 6):
            if chars[pos] == "O":
                chars[pos] = "0"
        text = "".join(chars)

    return text


def _split_and_squish(value: str, pattern: str) -> list[str]:
    return [part for part in (_squish(part) for part in re.split(pattern, value)) if part]


def _squish(value: str) -> str:
    return " ".join(value.split())


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _normalize_inorganic_active(value: str) -> str:
    match = _INORGANIC_ACTIVE_RE.search(value)
    if not match:
        if re.fullmatch(r"\s*phenol\s*", value, re.I):
            return "phenol"
        return ""
    return _basic_preserve_normalize(match.group(1))


def _strip_biosimilar_suffixes(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        base = match.group("base")
        base_tokens = base.split()
        stem = base_tokens[0] if base_tokens[-1] in {"alfa", "beta"} else base_tokens[-1]

        if stem in _BIOSIMILAR_SUFFIX_BASE_ALLOWLIST:
            return match.group(0)
        if (
            base.startswith("insulin ")
            or stem in _BIOSIMILAR_BASE_TOKENS
            or (len(stem) >= 5 and stem.endswith(_BIOSIMILAR_BASE_STEMS))
        ):
            return base
        return match.group(0)

    return _BIOSIMILAR_SUFFIX_RE.sub(replacement, value)


def _prefer_equivalence_target(value: str) -> str:
    match = _EQUIVALENCE_TARGET_RE.search(value)
    if not match:
        return value
    target = match.group(2).strip()
    if re.search(r"[a-z]", target, re.I):
        return target
    return value


def _normalize_radiopharma(value: str) -> str:
    text = value.lower()
    text = _REVERSE_ISOTOPE_RE.sub(
        lambda match: f"{match.group(2).lower()} {match.group(1).lower()}", text
    )
    text = _ISOTOPE_RE.sub(lambda match: f"{match.group(1).lower()} {match.group(2).lower()}", text)
    text = re.sub(r"\bc\s+(13|14)\s+urea\b", r"urea c \1", text)
    text = re.sub(
        r"\btechnetium\s+tc\s+99m\s+sodium\s+pertechnetate\b",
        "sodium pertechnetate tc 99m",
        text,
    )
    text = re.sub(
        r"\btechnetium\s+tc\s+99m\s+pertechnetate\b",
        "sodium pertechnetate tc 99m",
        text,
    )
    text = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|\u00b5g|ug|g|ml|l|%|w/v|v/v|w/w|iu|units?|mol)(\s*/\s*\w+)?\b", " ", text)
    text = re.sub(r"\b(kit|generator|dose|vial)\b", " ", text)
    text = text.translate(_PUNCT_TRANSLATION)
    text = _squish(text)
    text = re.sub(r"\bc\s+(13|14)\s+urea\b", r"urea c \1", text)
    text = re.sub(
        r"\btechnetium\s+tc\s+99m\s+(?:sodium\s+)?pertechnetate\b",
        "sodium pertechnetate tc 99m",
        text,
    )
    text = re.sub(r"\btechnetium\s+tc\s+99m\s+medronate\b", "technetium tc 99m medronic acid", text)
    text = re.sub(r"\btechnetium\s+tc\s+99m\s+pentetate\b", "technetium tc 99m pentetic acid", text)
    text = re.sub(r"\btechnetium\s+tc\s+99m\s+oxidronate\b", "technetium tc 99m oxidronic acid", text)
    text = re.sub(r"\bindium\s+in\s+111\s+pentetate(?:\s+disodium)?\b", "indium in 111 pentetic acid", text)
    text = re.sub(r"\bgallium\s+citrate(?:\s+eqv)?\s+ga\s+67\b", "gallium ga 67 citrate", text)
    text = re.sub(r"\brubidium\s+chloride\s+rb\s+82\b", "rubidium rb 82 chloride", text)
    text = re.sub(r"\balbumin\s+iodinated\s+i\s+(125|131)\s+serum\b", r"iodine i \1 human albumin", text)
    return text


def _basic_preserve_normalize(value: str) -> str:
    text = value.lower()
    text = text.replace("\u03bc", "u").replace("\u00b5", "u")
    text = re.sub(r"\baluminium\b", "aluminum", text)
    text = re.sub(r"\bsulphate\b", "sulfate", text)
    text = re.sub(r"\bsulphide\b", "sulfide", text)
    text = re.sub(r"\bsulphur\b", "sulfur", text)
    text = re.sub(r"\bsodium\s+hydrogen\s+carbonate\b", "sodium bicarbonate", text)
    text = re.sub(r"\bsodium\s+dihydrogen\s+phosphate\b", "sodium phosphate", text)
    text = re.sub(r"\bpotassium\s+dihydrogen\s+phosphate\b", "potassium phosphate", text)
    text = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|ug|g|ml|l|%|iu|units?)\b", " ", text)
    text = text.translate(_PUNCT_TRANSLATION)
    text = re.sub(r"\d+", " ", text)
    return _squish(text)
