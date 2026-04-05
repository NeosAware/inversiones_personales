import argparse
from getpass import getpass

import keyring


def parse_args():
    parser = argparse.ArgumentParser(description="Guarda secretos del robot bancario en Windows Credential Manager.")
    parser.add_argument("--job", required=True, help="ID del trabajo del robot.")
    parser.add_argument("--name", required=True, help="Nombre del secreto, por ejemplo username o password.")
    parser.add_argument("--value", help="Valor del secreto. Si no se indica, se pide por consola.")
    return parser.parse_args()


def main():
    args = parse_args()
    value = args.value if args.value is not None else getpass(f"Valor para {args.name}: ")
    service = f"inversiones_personales.bank_robot.{args.job}"
    keyring.set_password(service, args.name, value)
    print(f"Se ha guardado '{args.name}' en Windows Credential Manager para el job '{args.job}'.")


if __name__ == "__main__":
    main()
