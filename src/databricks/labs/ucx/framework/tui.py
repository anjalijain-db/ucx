import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Prompts:
    def multi_select_from_dict(self, all_prompt: str, item_prompt: str, choices: dict[str, Any]) -> list[Any]:
        selected: list[Any] = []
        if self.question(all_prompt, default="no") == "yes":
            return selected
        dropdown = {"[DONE]": "done"} | choices
        while True:
            key = self.choice(item_prompt, list(dropdown.keys()))
            if key == "done":
                break
            selected.append(choices[key])
            del dropdown[key]
            if len(selected) == len(choices):
                # we've selected everything
                break
        return selected

    def choice_from_dict(self, text: str, choices: dict[str, Any], *, sort: bool = True) -> Any:
        key = self.choice(text, list(choices.keys()), sort=sort)
        return choices[key]

    def choice(self, text: str, choices: list[Any], *, max_attempts: int = 10, sort: bool = True) -> str:
        if sort:
            choices = sorted(choices, key=str.casefold)
        numbered = "\n".join(f"\033[1m[{i}]\033[0m \033[36m{v}\033[0m" for i, v in enumerate(choices))
        prompt = f"\033[1m{text}\033[0m\n{numbered}\nEnter a number between 0 and {len(choices) - 1}: "
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            res = int(self.question(prompt, valid_number=True))
            if res >= len(choices) or res < 0:
                print(f"\033[31m[ERROR] Out of range: {res}\033[0m\n")
                continue
            return choices[res]
        msg = f"cannot get answer within {max_attempts} attempt"
        raise ValueError(msg)

    def confirm(self, text: str, *, max_attempts: int = 10):
        answer = self.question(text, valid_regex=r"[Yy][Ee][Ss]|[Nn][Oo]", default="no", max_attempts=max_attempts)
        return answer.lower() == "yes"

    def question(
        self,
        text: str,
        *,
        default: str | None = None,
        max_attempts: int = 10,
        valid_number: bool = False,
        valid_regex: str | None = None,
        validate: Callable[[str], bool] | None = None,
    ) -> str:
        default_help = "" if default is None else f"\033[36m (default: {default})\033[0m"
        prompt = f"\033[1m{text}{default_help}: \033[0m"
        match_regex = None
        if valid_number:
            valid_regex = r"^\d+$"
        if valid_regex:
            match_regex = re.compile(valid_regex)
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            res = input(prompt)
            if res and validate:
                if not validate(res):
                    continue
            if res and match_regex:
                if not match_regex.match(res):
                    print(f"\033[31m[ERROR] Not a '{valid_regex}' match: {res}\033[0m\n")
                    continue
                return res
            if not res and default:
                return default
            if not res:
                continue
            return res
        msg = f"cannot get answer within {max_attempts} attempt"
        raise ValueError(msg)


class MockPrompts(Prompts):
    def __init__(self, patterns_to_answers: dict[str, str]):
        self._questions_to_answers = sorted(
            [(re.compile(k), v) for k, v in patterns_to_answers.items()], key=lambda _: len(_[0].pattern), reverse=True
        )

    def question(self, text: str, default: str | None = None, **_) -> str:
        logger.info(f"Asking prompt: {text}")
        for question, answer in self._questions_to_answers:
            if not question.search(text):
                continue
            if not answer and default:
                return default
            return answer
        mocked = f"not mocked: {text}"
        raise ValueError(mocked)
