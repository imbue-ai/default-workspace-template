"""The supervisord program reader the band tests, the app-manifest tests and the supervisord layout test share."""

import configparser
from pathlib import Path


def supervisord_commands(
    supervisord_conf: Path, kinds: tuple[str, ...]
) -> dict[str, str]:
    """Every section of the given ``kinds`` (``"program"``, ``"eventlistener"``) the config declares, by name, with its command.

    supervisord's config is an ini file, so ``configparser`` reads it directly: that skips the
    file's prose comments and folds continuation lines. Interpolation is off because
    supervisord's own ``%(ENV_x)s`` syntax is not configparser's.

    Programs live one per file under ``supervisord.conf.d/``, and ``configparser`` does not
    follow supervisord's ``[include]``, so the drop-ins are read after the main config, which
    reproduces supervisord's precedence. ``system/test_supervisord_layout.py`` pins that
    directory as the one the include glob names: reading only the main config would leave every
    check over the programs asserting over nothing while appearing to pass.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(supervisord_conf)
    parser.read(sorted((supervisord_conf.parent / "supervisord.conf.d").glob("*.conf")))
    prefixes = tuple(f"{kind}:" for kind in kinds)
    return {
        section.partition(":")[2]: parser[section].get("command", "")
        for section in parser.sections()
        if section.startswith(prefixes)
    }
