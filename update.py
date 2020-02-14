from typing import Tuple, List, Optional
import os
import json
import time
import itertools
import re
import gzip
import io
from collections import Counter
from bs4 import BeautifulSoup
import requests
import urllib.request
import glob


class UnparsableLeveling(Exception):
    pass


def grouper(iterable, n, fillvalue=None):
    """Collect data into fixed-length chunks or blocks"""
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)


def pairwise(iterable):
    """s -> (s0,s1), (s1,s2), (s2, s3), ..."""
    a, b = itertools.tee(iterable)
    next(b, None)
    return zip(a, b)


rattribute = r"((?:[A-Za-z'\-\.0-9]+[\s\-]*)+:)"
rflat = r": (.+)"
rscaling = r"(\(\+.+?\))"
rnumber = r"(\d+\.?\d*)"


class AttributeModifier(dict):
    def __init__(self, sign, values, units):
        super().__init__({
            "sign": sign,
            "values": values,
            "units": units,
        })


class Attribute(dict):
    def __init__(self, name: str, modifiers: List[AttributeModifier]):
        super().__init__({
            "attribute": name,
            "modifiers": modifiers
        })

    @classmethod
    def from_string(cls, name: str, split, verbose: bool = False):
        results = []

        # Parse out the scalings first because it works better
        print("FROM_STRING INPUT:", split)
        scalings = re.compile(rscaling).findall(split)
        if verbose:
            print("SCALINGS", scalings, split)
        for scaling in scalings:
            split = split.replace(scaling, '').strip()  # remove the scaling part of the string for processing later
        scalings = [x.strip() for x in scalings]
        for i, scaling in enumerate(scalings):
            s = Attribute._parse_scaling(scaling, num_levels=5)
            results.append(s)

        # Now parse out the flat damage info
        flat = re.compile(rflat).findall(split)
        flat = [x.strip().split(' + ') for x in flat]
        flat = [x for s in flat for x in s]  # flatten the inner split list
        if not flat:
            numbers = re.compile(rnumber).findall(split)
            # Check for the case of a just a very basic "1 / 2 / 3 / 4 / 5" string
            if split.count(" / ") == 4 and len(numbers) == 5:
                flat = [split]
            elif split.count(" / ") == 2 and len(numbers) == 3:
                flat = [split]
            # Check for the case of a just a very basic "60" string
            elif len(numbers) == 1 and str(numbers[0]) == split:
                flat = [split]
        if verbose:
            print("FLAT", flat, split)
        # assert len(flat) == 1  # don't enforce this
        for i, f in enumerate(flat):
            f = Attribute._parse_flat(f, num_levels=5)
            results.append(f)

        return cls(name=name, modifiers=results)

    @staticmethod
    def _parse_scaling(scaling, num_levels) -> AttributeModifier:
        if scaling.startswith('(') and scaling.endswith(')'):
            scaling = scaling[1:-1].strip()
        modifier = scaling[0]  # + or -
        scaling = scaling[1:].strip()
        if ' / ' in scaling:
            split = scaling.split(' / ')
        else:
            split = [scaling for _ in range(num_levels)]
        results = []
        for value in split:
            v = re.compile(rnumber).findall(value)
            if len(v) == 0:
                assert value == "Siphoning Strike stacks"
                unit = ''
                v = value
            else:
                assert len(v) >= 1  # len(v) == 1 fails on e.g. "(+ 0.5% per 100 AP)" but we still just want the first #
                v = v[0]
                assert value.startswith(v) or value.startswith(f'[ {v}')  # 2nd one is for Vi's Denting Blows: "Bonus Physical Damage: 4 / 5.5 / 7 / 8.5 / 10% (+[ 1% per 35 ][ 2.86% per 100 ]bonus AD) of target's maximum health"
                unit = value[len(v):]
                v = eval(v)
            results.append((v, unit))
        results = AttributeModifier(sign=modifier, values=[v for v, unit in results], units=[unit for v, unit in results])
        return results

    @staticmethod
    def _parse_flat(flat, num_levels) -> AttributeModifier:
        if '(based on level)' in flat or '(based on casts)' in flat:
            if '(based on level)' in flat:
                unit = 'by level'
                flat = flat.replace('(based on level)', '').strip()
            elif '(based on casts)' in flat:
                unit = 'by cast'
                flat = flat.replace('(based on casts)', '').strip()
            else:
                raise RuntimeError("impossible")
            values = re.compile(rnumber).findall(flat)
            assert len(values) == 2
            minn = eval(values[0])
            maxx = eval(values[1])
            delta = (maxx - minn) / 17.0
            values = [minn + i*delta for i in range(18)]
            units = [unit for _ in range(18)]
            results = AttributeModifier(sign="+", values=values, units=units)
            return results
        else:
            if flat.startswith('(') and flat.endswith(')'):
                flat = flat[1:-1].strip()
            if ' / ' in flat:
                split = flat.split(' / ')
            else:
                split = [flat for _ in range(num_levels)]
            results = []
            for value in split:
                v = re.compile(rnumber).findall(value)
                assert len(v) == 1
                v = v[0]
                assert value.startswith(v)
                unit = value[len(v):]
                v = eval(v)
                results.append((v, unit))

            values = [v for v, unit in results]
            units = [unit for v, unit in results]
            unique_units = set(units)
            if '' in unique_units:
                unique_units.remove('')
            if unique_units:
                assert len(unique_units) == 1
                unit = next(iter(unique_units))
                units = [unit for _ in range(len(units))]
            results = AttributeModifier(sign="+", values=values, units=units)
            return results


class Ability(dict):
    @classmethod
    def from_html(cls, champion_name: str, ability_name: str, verbose: bool = False):
        data = Ability._pull_champion_ability(champion_name, ability_name, verbose=verbose)
        self = cls()
        self.update(data)
        return self

    @staticmethod
    def _pull_champion_ability(champion_name, ability_name, verbose: bool = False):
        ability_name = ability_name.replace(' ', '_')

        # Pull the html from the wiki
        url = f"https://leagueoflegends.fandom.com/wiki/Template:Data_{champion_name}/{ability_name}"
        html = download_webpage(url)
        soup = BeautifulSoup(html, 'html5lib')

        table = soup.find_all(['th', 'td'])

        # Set some fields to ignore
        exclude_parameters = { "callforhelp", "flavorsound", "video", "video2", "yvideo", "yvideo2", "flavor sound", "video 2", "YouTube video", 'YouTube video 2', "Not applicable to be stolen.", "Stealable", "All maps",
            # Bard
            "15", "30", "45", "55", "60", "75", "90", "100", "145", "190", "235", "280", "325", "Chimes", "3:20", "Meep limit increased to 2.", "9:10", "Slow increased to 35%.", "15:50", "Recharge time reduced to 6 seconds.", "21:40", "Recharge time reduced to 5 seconds.", "28:20", "Recharge time reduced to 4 seconds.", "34:10", "Slow increased to 75%.", "40:50", "Meep limit increased to 9.", "Displays additional information with effect table to the right.",
            # Pyke
            "25", "80", "400", "650", "800", "900", "950", "1000", "1200", "2100", "2500", "2600", "2750", "3000", "3733", "Abyssal Mask Abyssal Mask", "All maps", "Black Cleaver Black Cleaver", "32.1", "Catalyst of Aeons Catalyst of Aeons", "21.4", "Dead Man's Plate Dead Man's Plate", "13.7", "Doran's Shield Doran's Shield", "Summoner's Rift", "78.2", "Frostfang Frostfang", "Guardian's Hammer Guardian's Hammer", "Howling Abyss", "10.7", "Harrowing Crescent Harrowing Crescent", "14.3", "Infernal Mask Infernal Mask", "29.3", "Knight's Vow Knight's Vow", "Oblivion Orb Oblivion Orb", "99.3", "Phage Phage", "28.6", "Relic Shield Relic Shield", "Rod of Ages (Quick Charge) Rod of Ages (Quick Charge)", "Rylai's Crystal Scepter Rylai's Crystal Scepter", "Shurelya's Reverie Shurelya's Reverie", "5.7", "Spellthief's Edge Spellthief's Edge", "Sterak's Gage Sterak's Gage", "30.4", "Thornmail Thornmail", "72.1", "Trinity Fusion Trinity Fusion", "57.1",
            # Zoe
            "Mercurial Scimitar", "Randuin's Omen", "Hextech Protobelt-01", "Youmuu's Ghostblade", "Black Mist Scythe", "Runesteel Spaulders", "Edge of Night", "Targon's Buckler", "Pauldrons of Whiterock",
        }

        # We might want to ignore these, not sure yet
        maybe = {
            "custominfo",
            "recharge",
            "customlabel",
            "additional",
        }

        # Do a little html modification based on the "viewsource"
        strip_table = [item.text.strip() for item in table]
        start = strip_table.index("Parameter")+3
        table = table[start:]
        return Ability._parse_html_table(table, exclude_parameters, verbose=verbose)

    @staticmethod
    def _parse_html_table(table, exclude_parameters, verbose: bool = False):
        # Iterate over the data in the table and parse the info
        data = {}
        for i, (parameter, value, desc) in enumerate(grouper(table, 3)):
            if not value:
                continue
            if i == 0:  # parameter is '1' for some reason but it's the ability name
                parameter = "name"
            else:
                parameter = parameter.text.strip()
            # desc = desc.text.strip()
            text = value.text.strip()
            if text and parameter not in exclude_parameters:
                data[parameter] = value

        skill = data['skill'].text.strip()
        for parameter, value in data.items():
            if parameter.startswith('leveling') and skill in ['Q', 'W', 'E', 'R']:
                try:
                    value = Ability._parse_leveling(str(value), skill, verbose=verbose)
                except UnparsableLeveling:
                    if verbose:
                        print(f"WARNING! Could not parse: {value.text.strip()}")
                    value = value.text.strip()
                if verbose:
                    print("PARSED:", value)
                data[parameter] = value
            elif parameter == "cooldown":
                parsed = Ability._preparse_format(value)
                if "(based on level)" in parsed and " / " in parsed:
                    parsed = parsed.replace("(based on level)", "").strip()
                elif "(based on  Phenomenal Evil stacks)" in parsed:
                    data[parameter] = parsed
                    continue
                data[parameter] = Attribute.from_string(parameter, parsed, verbose=verbose)
            elif parameter == "cost":
                parsed = Ability._preparse_format(value)
                if "10 Moonlight + 60" in parsed:
                    data[parameter] = parsed
                    continue
                data[parameter] = Attribute.from_string(parameter, parsed, verbose=verbose)
            else:
                data[parameter] = value.text.strip()
        if verbose:
            print(data)
        if verbose:
            print()
        return data

    @staticmethod
    def _preparse_format(leveling: str):
        if not isinstance(leveling, str):
            leveling = str(leveling)
        leveling = leveling.replace('</dt>', ' </dt>')
        leveling = leveling.replace('</dd>', ' </dd>')
        leveling = BeautifulSoup(leveling, 'html5lib')
        parsed = leveling.text.strip()
        parsed = parsed.replace(u'\xa0', u' ')
        return parsed

    @staticmethod
    def _parse_leveling(leveling: str, skill: str, verbose: bool = False):
        parsed = Ability._preparse_format(leveling)
        if verbose:
            print("PARSING LEVELING:", str(parsed))

        results = Ability._split_leveling(parsed, verbose=verbose)

        if skill == 'R':
            if verbose:
                print("PREPARSED:", results)
            for i, attribute in enumerate(results):
                for j, modifier in enumerate(attribute['modifiers']):
                    mvalues = modifier['values']
                    if len(mvalues) == 5:
                        modifier['values'] = [mvalues[0], mvalues[2], mvalues[4]]
                    munits = modifier['units']
                    if len(munits) == 5:
                        modifier['units'] = [munits[0], munits[2], munits[4]]

        return results

    @staticmethod
    def _split_leveling(leveling: str, verbose: bool = False) -> List[Attribute]:
        # Remove some weird stuff

        leveling_removals = list()
        #  Ekko Chronobreak
        leveling_removals.append('(increased by 3% per 1% of health lost in the past 4 seconds)')

        for removal in leveling_removals:
            if removal in leveling:
                leveling = leveling.replace(removal, '').strip()

        # Split the leveling into separate attributes
        matches, splits = Ability._match_and_split(leveling, rattribute)

        # Parse those attributes into a usable format
        results = []
        if verbose:
            print("SPLITS", splits)
        for attribute_name, split in zip(matches, splits):
            if verbose:
                print("ATTRIBUTE", attribute_name)

            attribute = Attribute.from_string(attribute_name, split, verbose=verbose)
            results.append(attribute)
        return results

    @staticmethod
    def _match_and_split(string: str, regex: str) -> Tuple[Optional[List], Optional[List]]:
        if string == "Pounce scales with  Aspect of the Cougar's rank":
            raise UnparsableLeveling(string)
        elif string == "Cougar form's abilities rank up when  Aspect of the Cougar does":
            raise UnparsableLeveling(string)
        matches = re.compile(regex).findall(string)
        matches = [match[:-1] for match in matches]  # remove the trailing :

        splits = []
        for i, m in enumerate(matches[1:], start=1):
            start = string[len(matches[i-1]):].index(m)
            split = string[:len(matches[i-1])+start].strip()
            splits.append(split)
            string = string[len(matches[i-1])+start:]
        splits.append(string)

        # Heimer has some scalings that start with numbers...
        if splits == ['Initial Rocket Magic Damage: 135 / 180 / 225 (+ 45% AP) 2-5', 'Rocket Magic Damage: 32 / 45 / 58 (+ 12% AP) 6-20', '0 Rocket Magic Damage: 16 / 22.5 / 29 (+ 6% AP)', 'Total Magic Damage: 503 / 697.5 / 892 (+ 183% AP)', ') Total Minion Magic Damage: 2700 / 3600 / 4500 (+ 900% AP)']:
            splits = ['Initial Rocket Magic Damage: 135 / 180 / 225 (+ 45% AP)', '2-5 Rocket Magic Damage: 32 / 45 / 58 (+ 12% AP)', '6-20 Rocket Magic Damage: 16 / 22.5 / 29 (+ 6% AP)', 'Total Magic Damage: 503 / 697.5 / 892 (+ 183% AP)', 'Total Minion Magic Damage: 2700 / 3600 / 4500 (+ 900% AP)']

        return matches, splits


def pull_all_champion_stats():
    # Download the page source
    url = "https://leagueoflegends.fandom.com/wiki/Module:ChampionData/data"
    html = download_webpage(url)
    soup = BeautifulSoup(html, 'html5lib')

    # Parse out the data
    spans = soup.find_all('span')
    start = None
    for i, span in enumerate(spans):
        if str(span) == '<span class="kw1">return</span>':
            start = i
    spans = spans[start:]
    data = ""
    brackets = Counter()
    for span in spans:
        text = span.text
        if text == "{" or text == "}":
            brackets[text] += 1
        if brackets["{"] != 0:
            data += text
        if brackets["{"] == brackets["}"] and brackets["{"] > 0:
            break
    # Reformat the data
    data = data.replace('=', ':')
    data = data.replace('["', '"')
    data = data.replace('"]', '"')
    data = data.replace('[1]', '1')
    data = data.replace('[2]', '2')
    data = data.replace('[3]', '3')
    data = data.replace('[4]', '4')
    data = data.replace('[5]', '5')
    data = data.replace('[6]', '6')
    data = eval(data)
    return data


#NONASCII = Counter()
def download_webpage(url):
    page = requests.get(url)
    html = page.content.decode(page.encoding)
    soup = BeautifulSoup(html, 'html5lib')
    html = str(soup)
    html = html.replace(u'\u00a0', u' ')
    html = html.replace(u'\u300c', u'[')
    html = html.replace(u'\u300d', u']')
    html = html.replace(u'\u00ba', u'°')
    html = html.replace(u'\u200b', u'')  # zero width space
    html = html.replace(u'\u200e', u'')  # left-to-right mark
    html = html.replace(u'\xa0', u' ')
    #html = html.replace(u'‐', u'-')
    #html = html.replace(u'−', u'-')
    #html = html.replace(u'☂', u'')
    #html = html.replace(u'•', u'*')
    #html = html.replace(u'’', u'')
    #html = html.replace(u'↑', u'')
    #html = html.replace(u'…', u'...')
    #html = html.replace(u'↑', u'')
    #NON-ASCII CHARACTERS: Counter({'…': 130, '°': 76, '×': 74, '–': 28, '÷': 20, '∞': 18, '\u200e': 8, '≈': 4, '≤': 2})

    #for a in html:
    #    if ord(a) > 127:
    #        NONASCII[a] += 1
    #if NONASCII:
    #    print("NON-ASCII CHARACTERS:", NONASCII)

    assert u'\xa0' not in html
    return html


def save_json(data, filename):
    def set_default(obj):
        if isinstance(obj, set):
            return list(obj)
        raise TypeError(f"Cannot serialize object of type: {type(obj)} ... {obj}")
    sdata = json.dumps(data, indent=2, default=set_default)
    with open(filename, 'w') as of:
        of.write(sdata)
    with open(filename, 'r') as f:
        sdata = f.read()
        sdata = sdata.replace(u'\u00a0', u' ')
        sdata = sdata.replace(u'\u300d', u' ')
        sdata = sdata.replace(u'\u300c', u' ')
        sdata = sdata.replace(u'\u00ba', u' ')
        sdata = sdata.replace(u'\xa0', u' ')
    with open(filename, 'w') as of:
        of.write(sdata)


def main():
    statsfn = "data/champion_stats.json"
    stats = pull_all_champion_stats()
    save_json(stats, statsfn)

    with open(statsfn) as f:
        stats = json.load(f)

    # Missing skills
    missing_skills = {
        "Annie": ["Command Tibbers"] ,
        "Jinx": ["Switcheroo! 2"] ,
        "Nidalee": ["Aspect of the Cougar 2"] ,
        "Pyke": ["Death from Below 2"],
        "Rumble": ["Electro Harpoon 2"] ,
        "Shaco": ["Command Hallucinate"] ,
        "Syndra": ["Force of Will 2"] ,
        "Taliyah": ["Seismic Shove 2"],
    }

    for champion_name, details in stats.items():
        jsonfn = f"data/_{details['apiname']}.json"
        #if os.path.exists(jsonfn):
        #    continue
        print(champion_name)
        if champion_name == "Kled & Skaarl":
            champion_name = "Kled"
        for ability in ['i', 'q', 'w', 'e', 'r']:
            result = {}
            for ability_name in details[f"skill_{ability}"].values():
                if champion_name in missing_skills and ability_name in missing_skills[champion_name]:
                    continue
                print(ability_name)
                r = Ability.from_html(champion_name, ability_name, verbose=True)
                # check to see if this ability was already pulled
                found = False
                for r0 in result.values():
                    if r == r0:
                        found = True
                if not found:
                    result[ability_name] = r
            details[f"skill_{ability}"] = result
        save_json(details, jsonfn)
        print()



def rename_keys(j):
    map = {
        "id": "id",
        "champion": "champion",
        "skill": "skill",
        "name": "name",
        "apiname": "apiName",
        "fullname": "fullName",
        "nickname": "nickname",
        "title": "title",
        "attack": "attack",
        "defense": "defense",
        "magic": "magic",
        "difficulty": "difficulty",
        "herotype": "heroType",
        "alttype": "altType",
        "resource": "resource",
        "stats": "stats",
        "hp_base": "hpBase",
        "hp_lvl": "hpPerLevel",
        "mp_base": "manaBase",
        "mp_lvl": "manaPerLevel",
        "arm_base": "armorBase",
        "arm_lvl": "armorPerLevel",
        "mr_base": "magicResistBase",
        "mr_lvl": "magicResistPerLevel",
        "hp5_base": "hpPer5Base",
        "hp5_lvl": "hpPer5PerLevel",
        "mp5_base": "manaPer5Base",
        "mp5_lvl": "manaPer5PerLevel",
        "dam_base": "damageBase",
        "dam_lvl": "damagePerLevel",
        "as_base": "attackSpeedBase",
        "as_lvl": "attackSpeedPerLevel",
        "crit_base": "critBase",
        "crit_mod": "critModifier",
        "missile_speed": "missileSpeed",
        "attack_cast_time": "attackCastTime",
        "attack_total_time": "attackTotalTime",
        "windup_modifier": "windupModifier",
        "urf_dmg_dealt": "urfDmgDealt",
        "urf_dmg_taken": "urfDmgTaken",
        "urf_healing": "urfHealing",
        "urf_shielding": "urfShielding",
        "aram_dmg_dealt": "aramDmgDealt",
        "aram_dmg_taken": "aramDmgTaken",
        "aram_healing": "aramHealing",
        "aram_shielding": "aramShielding",
        "static": "static",
        "icon": "icon",
        "gameplay_radius": "gameplayRadius",
        "description": "description",
        "description2": "description2",
        "description3": "description3",
        "targeting": "targeting",
        "affects": "affects",
        "damagetype": "damageType",
        "spelleffects": "spellEffects",
        "spellshield": "spellshield",
        "notes": "notes",
        "range": "range",
        "range_lvl": "rangePerLevel",
        "ms": "movespeed",
        "acquisition_radius": "acquisitionRadius",
        "selection_radius": "selectionRadius",
        "pathing_radius": "pathingRadius",
        "as_ratio": "attackSpeedRatio",
        "attack_delay_offset": "attackDelayOffset",
        "aram_dmg_taken": "aramDamageTaken",
        "rangetype": "rangeType",
        "date": "date",
        "patch": "patch",
        "changes": "changes",
        "role": "role",
        "damage": "damage",
        "toughness": "toughness",
        "control": "control",
        "mobility": "mobility",
        "utility": "utility",
        "style": "style",
        "adaptivetype": "adaptiveType",
        "be": "blueEssense",
        "rp": "rp",
        "skill_i": "skillP",
        "skill_q": "skillQ",
        "skill_w": "skillW",
        "skill_e": "skillE",
        "skill_r": "skillR",
        "secondary attributes": "secondaryAttributes",
    }

    new = {}
    for key, value in j.items():
        if key.startswith("skill_"):
            value = list(value.values())
        elif key == "skill" and value == "I":
            value = "P"

        if isinstance(value, dict):
            value = rename_keys(value)
        try:
            new_key = map[key]
            new[new_key] = value
        except:
            print(key, value)
            new_key = map[key]
            new[new_key] = value
    return new


def rename_all():
    files = sorted(glob.glob(f"data/_**.json"))
    for fn in files:
        with open(fn) as f:
            j = json.load(f)
        renamed = rename_keys(j)
        new_fn = fn.replace('data/_', 'data/')
        save_json(renamed, new_fn)


if __name__ == "__main__":
    main()
    rename_all()
