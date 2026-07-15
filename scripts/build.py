import subprocess

import clean


def _invoke_uv_build():
    subprocess.run(["uv", "build", "--clear"], check=True)


def main():
    clean.clean_venv_scripts()
    _invoke_uv_build()


if __name__ == "__main__":
    main()
