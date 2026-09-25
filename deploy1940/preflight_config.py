r"""
Check that every $variable in the resolved config can be expanded.

Deliberately stricter than the release's own validator. driver.py:164 uses

    VARIABLE_RE = re.compile(r"([$][A-Za-z0-9_]+)")

which does not match the ${VAR} form at all, while the code that actually
performs the substitution, ctools/paths.py:15, uses

    re.compile('(\$\{\w*\})|(\$\w*)')

and matches both. Anything written as ${VAR} is therefore invisible to
config_validate and can only fail later, at the point of use, deep inside a
Spark task. This script uses the paths.py pattern so those are caught up front.
"""
import os
import re
import sys

from das_framework.ctools.hierarchical_configparser import HierarchicalConfigParser

SUBST_RE = re.compile(r'(\$\{\w*\})|(\$\w*)')     # ctools/paths.py:15
DRIVER_RE = re.compile(r"([$][A-Za-z0-9_]+)")     # driver.py:164
IGNORE = {"APPLICATIONID"}
CFG = "configs/Census1940/DDP2010_Update/ipums_1940_local.ini"


def var_names(value):
    out = []
    for braced, bare in SUBST_RE.findall(value):
        name = braced[2:-1] if braced else bare[1:]
        if name:
            out.append(name)
    return out


def main():
    cfg = HierarchicalConfigParser()
    cfg.read(CFG)

    bad = []
    for section in cfg.sections():
        for option in cfg.options(section):
            value = cfg.get(section, option)
            for name in var_names(value):
                if name in os.environ or name in IGNORE:
                    continue
                invisible = f"${name}" not in DRIVER_RE.findall(value)
                bad.append((section, option, name, value.strip()[:70], invisible))

    for section, option, name, value, invisible in bad:
        note = "  (invisible to driver.py config_validate)" if invisible else ""
        print(f"   UNDEFINED ${name}: [{section}] {option} = {value}{note}")

    if bad:
        return 1
    print("   all config $variables resolve (checked ${VAR} and $VAR forms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
