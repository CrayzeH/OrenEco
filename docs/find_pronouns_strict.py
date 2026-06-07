import json
import re
from collections import Counter

FORMS = """
я меня мне мной мною
ты тебя тебе тобой тобою
мы нас нам нами
вы вас вам вами
он она оно они его ему им ним нем их ими ними ней нее ею ей
себя себе собой собою
мой моя мое мои моего моей моему моим моими моих мою
твой твоя твое твои твоего твоей твоему твоим твоими твоих твою
наш наша наше наши нашего нашей нашему нашим нашими наших нашу
ваш ваша ваше ваши вашего вашей вашему вашим вашими ваших вашу
свой своя свое свои своего своей своему своим своими своих свою
этот эта это эти этого этой этому этим этими этих эту
тот та то те того той тому тем теми тех ту
такой такая такое такие такого такой такому таким такими таких такую
какой какая какое какие какого какой какому каким какими каких какую
который которая которое которые которого которой которому которым которыми которых которую
чей чья чье чьи чьего чьей чьему чьим чьими чьих чью
кто кого кому кем ком что чего чему чем
сколько столько несколько
весь вся все всего всей всему всем всеми всех всю
каждый каждая каждое каждые каждого каждой каждому каждым каждыми каждых каждую
сам сама само сами самого самой самому самим самими самих саму
иной иная иное иные иного иной иному иным иными иных иную
любой любая любое любые любого любой любому любым любыми любых любую
некто нечто некоторый некоторая некоторое некоторые некоторого некоторой некоторому некоторым некоторыми некоторых некоторую
никто ничто никакой никакая никакое никакие никакого никакой никакому никаким никакими никаких никакую
данный данная данное данные данного данной данному данным данными данных данную
"""

pronouns = {word for word in FORMS.split() if not word.startswith("данн")}
pattern = re.compile(
    r"(?iu)(?<![А-Яа-яЁёA-Za-z0-9_])("
    + "|".join(map(re.escape, sorted(pronouns, key=len, reverse=True)))
    + r")(?![А-Яа-яЁёA-Za-z0-9_])"
)

rows = []
counts = Counter()

with open("docs/paragraphs_pages.jsonl", encoding="utf-8-sig") as f:
    for line in f:
        obj = json.loads(line)
        text = obj["text"]
        normalized = text.replace("ё", "е").replace("Ё", "Е")
        found = [m.group(1) for m in pattern.finditer(normalized)]
        if not found:
            continue
        counts.update(x.lower() for x in found)
        fragment = text[:260].replace("\n", " ")
        if len(text) > 260:
            fragment += "..."
        rows.append((obj["page"], obj["paragraph"], sorted(set(found), key=str.lower), fragment))

with open("docs/pronouns_report_strict.txt", "w", encoding="utf-8") as out:
    out.write(f"Найдено абзацев: {len(rows)}\n")
    out.write(f"Найдено употреблений: {sum(counts.values())}\n\n")
    out.write("Частые формы:\n")
    for word, count in counts.most_common(30):
        out.write(f"{word}: {count}\n")
    out.write("\n")
    for page, paragraph, found, fragment in rows:
        out.write(f"Страница {page}, абзац {paragraph}: {', '.join(found)}\n")
        out.write(f"{fragment}\n\n")

with open("docs/pronouns_short_strict.txt", "w", encoding="utf-8") as out:
    for page, paragraph, found, _fragment in rows:
        out.write(f"Страница {page}, абзац {paragraph}: {', '.join(found)}\n")

print(f"rows={len(rows)} occurrences={sum(counts.values())} unique={len(counts)}")
print(counts.most_common(20))
