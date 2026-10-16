import subprocess


def run_user_command(argument):
    return subprocess.run(argument, shell=True)