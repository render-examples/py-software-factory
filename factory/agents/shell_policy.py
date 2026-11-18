from __future__ import annotations

import re
from pathlib import PurePosixPath

READ_ONLY_GIT = frozenset(
    {
        "blame",
        "cat-file",
        "describe",
        "diff",
        "diff-tree",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)
NETWORK = frozenset({"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "socat"})
PRIVILEGE = frozenset({"sudo", "su", "doas"})
PACKAGE_RUNNERS = frozenset({"npx", "bunx"})
INSTALL_VERBS = {
    "npm": {"install", "i", "ci", "add", "update", "exec"},
    "pnpm": {"install", "i", "add", "update", "dlx"},
    "yarn": {"install", "add", "dlx"},
    "bun": {"install", "i", "add"},
    "pip": {"install"},
    "pip3": {"install"},
    "uv": {"install", "add", "sync"},
    "gem": {"install"},
    "cargo": {"install"},
    "go": {"get", "install"},
    "apt-get": {"install"},
    "apt": {"install"},
    "apk": {"add"},
    "brew": {"install"},
}
FILE_WRITERS = frozenset(
    {"rm", "rmdir", "mv", "cp", "mkdir", "touch", "ln", "truncate", "tee", "patch", "chmod", "chown"}
)
WRAPPERS = frozenset({"env", "timeout", "nice", "xargs", "command", "exec", "time", "nohup"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
REDIRECT = re.compile(r"^(\d*|&)(?:>\||>>?)(.*)$")
WRAPPER_OPTIONS_WITH_VALUE = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "xargs": frozenset(
        {
            "-a",
            "--arg-file",
            "-E",
            "--eof",
            "-I",
            "--replace",
            "-L",
            "--max-lines",
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-s",
            "--max-chars",
        }
    ),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
}
GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})


def reject_reason(command: str, scratch_dir: str) -> str | None:
    """Return why a read-only command is rejected. This is guidance, not a security boundary."""
    for words in _split_commands(command):
        if reason := _check_command(words, scratch_dir):
            return reason
    return None


def _in_scratch(path: str, scratch_dir: str) -> bool:
    if ".." in path.split("/"):
        return False
    if path == "/dev/null" or path == scratch_dir or path.startswith(f"{scratch_dir}/"):
        return True
    return re.match(r"^(/tmp|\$\{?(TMPDIR|HOME)\}?|~)(/|$)", path) is not None


def _skip_wrapper(words: list[str], index: int, wrapper: str, scratch_dir: str) -> tuple[int, str | None]:
    index += 1
    value_options = WRAPPER_OPTIONS_WITH_VALUE.get(wrapper, frozenset())
    while index < len(words):
        argument = words[index]
        if argument == "--":
            index += 1
            break
        option = next(
            (
                candidate
                for candidate in value_options
                if argument == candidate
                or argument.startswith(f"{candidate}=")
                or (candidate.startswith("-") and not candidate.startswith("--") and argument.startswith(candidate))
            ),
            None,
        )
        if option is not None:
            attached = argument != option
            if "=" in argument:
                value = argument.split("=", 1)[1]
            elif attached:
                value = argument[len(option) :]
            else:
                value = ""
            if not attached:
                index += 1
                if index >= len(words):
                    return index, f"{wrapper} {option} requires a value"
                value = words[index]
            if wrapper == "time" and option in {"-o", "--output"} and not _in_scratch(value, scratch_dir):
                return index, f"time output is only allowed in {scratch_dir}"
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        break
    if wrapper == "timeout" and index < len(words) and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[index]):
        index += 1
    if wrapper == "env":
        while index < len(words) and re.match(r"^[A-Za-z_]\w*=", words[index]):
            index += 1
    return index, None


def _check_command(raw: list[str], scratch_dir: str) -> str | None:
    words: list[str] = []
    index = 0
    while index < len(raw):
        match = REDIRECT.match(raw[index])
        if not match:
            words.append(raw[index])
            index += 1
            continue
        target = match.group(2)
        if not target:
            index += 1
            target = raw[index] if index < len(raw) else ""
        if not re.match(r"^&\d+$", target) and not _in_scratch(target, scratch_dir):
            return (
                f"redirecting output to {target or 'a file'} is not allowed: only files in {scratch_dir} may be written"
            )
        index += 1

    index = 0
    while index < len(words):
        if re.match(r"^[A-Za-z_]\w*=", words[index]):
            index += 1
            continue
        wrapper = PurePosixPath(words[index]).name
        if wrapper not in WRAPPERS:
            break
        index, reason = _skip_wrapper(words, index, wrapper, scratch_dir)
        if reason:
            return reason
    name = PurePosixPath(words[index]).name if index < len(words) else ""
    args = words[index + 1 :]
    if not name:
        return None
    if name in NETWORK:
        return f"{name} is not allowed: read-only agents have no network tools"
    if name in PRIVILEGE:
        return f"{name} is not allowed: privilege escalation is not allowed"
    if name in PACKAGE_RUNNERS:
        return f"{name} is not allowed: read-only agents don't install or download packages"
    verb = next((arg for arg in args if not arg.startswith("-")), None)
    if verb and verb in INSTALL_VERBS.get(name, set()):
        return f"{name} {verb} is not allowed: read-only agents don't install packages"
    if name == "git":
        if any(arg == "-c" or arg.startswith(("--config-env", "--exec-path")) for arg in args):
            return "git config overrides are not allowed: they can run arbitrary commands"
        sub_index = 0
        while sub_index < len(args):
            option = args[sub_index]
            if option == "--":
                sub_index += 1
                break
            option_name = option.split("=", 1)[0]
            if option_name in GIT_GLOBAL_OPTIONS_WITH_VALUE:
                sub_index += 1 if "=" in option else 2
                continue
            if option.startswith("-"):
                sub_index += 1
                continue
            break
        subcommand = args[sub_index] if sub_index < len(args) else None
        if subcommand and subcommand not in READ_ONLY_GIT:
            return f"git {subcommand} is not allowed: this agent is read-only"
    in_place = name in {"sed", "perl"} and any(
        re.match(r"^-[a-zA-Z]*i", arg) or arg.startswith("--in-place") for arg in args
    )
    if name in FILE_WRITERS or in_place:
        paths = [arg for arg in args if not arg.startswith("-")]
        targets = paths[-1:] if name in {"cp", "ln"} else paths[1:] if name in {"chmod", "chown"} or in_place else paths
        allowed = (name == "tee" and not targets) or (
            bool(targets) and all(_in_scratch(path, scratch_dir) for path in targets)
        )
        if not allowed:
            return f"{name} is only allowed on files in {scratch_dir}: this agent is read-only"
    if name in SHELLS and "-c" in args:
        command_index = args.index("-c") + 1
        return reject_reason(args[command_index] if command_index < len(args) else "", scratch_dir)
    if name == "eval":
        return reject_reason(" ".join(args), scratch_dir)
    return None


def _split_commands(line: str) -> list[list[str]]:
    commands: list[list[str]] = []
    words: list[str] = []
    word = ""
    in_word = False
    quote: str | None = None
    opened: list[tuple[str, str | None]] = []

    def end_word() -> None:
        nonlocal word, in_word
        if in_word:
            words.append(word)
        word = ""
        in_word = False

    def end_command() -> None:
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                word += char
            index += 1
            continue
        if char == "\\" and index + 1 < len(line):
            index += 1
            word += line[index]
            in_word = True
        elif not quote and opened and char == opened[-1][0]:
            end_command()
            _, quote = opened.pop()
            in_word = quote is not None
        elif char == "`" or (char == "$" and index + 1 < len(line) and line[index + 1] == "("):
            end_command()
            opened.append(("`" if char == "`" else ")", quote))
            quote = None
            if char == "$":
                index += 1
        elif quote == '"':
            if char == '"':
                quote = None
            else:
                word += char
        elif char in {"'", '"'}:
            quote = char
            in_word = True
        elif char.isspace() and char != "\n":
            end_word()
        elif char in ";&|()\n" and not (
            (char == "&" and line[index + 1 : index + 2] == ">")
            or (char in {"&", "|"} and line[index - 1 : index] == ">")
        ):
            end_command()
        else:
            word += char
            in_word = True
        index += 1
    end_command()
    return commands
