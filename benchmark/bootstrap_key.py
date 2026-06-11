#!/usr/bin/env python3
"""
Mint an Ecolyxis API key for running the intelligence benchmark, and make sure
the owning user's wallet has enough balance to pay for the run.

Must be run with the app's virtualenv from the project root:

    cd /opt/Ecolyxis
    venv/bin/python benchmark/bootstrap_key.py --email you@example.com --topup-pence 500

If --email is omitted it uses the first user in the database. It prints the raw
key (shown once) to stdout; export it as ECOLYXIS_API_KEY for the benchmark.

This mutates the live database (creates an ApiKey row, optionally credits the
Wallet). Nothing is charged to Stripe — wallet balance is internal pence.
"""
import argparse
import sys

sys.path.insert(0, ".")

from app import create_app, db                       # noqa: E402
from app.models import User, ApiKey, Wallet          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="user to own the key (default: first user)")
    ap.add_argument("--name", default="intelligence-benchmark", help="API key label")
    ap.add_argument("--topup-pence", type=int, default=0,
                    help="ensure wallet balance is at least this many pence")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        if args.email:
            user = User.query.filter_by(email=args.email).first()
        else:
            user = User.query.order_by(User.id.asc()).first()
        if not user:
            sys.exit("No matching user found.")

        raw_key, key_hash, prefix = ApiKey.generate_key()
        db.session.add(ApiKey(user_id=user.id, name=args.name,
                              key_hash=key_hash, key_prefix=prefix))

        wallet = Wallet.query.filter_by(user_id=user.id).first()
        if not wallet:
            wallet = Wallet(user_id=user.id, balance_pence=0)
            db.session.add(wallet)
            db.session.flush()
        if args.topup_pence and wallet.balance_pence < args.topup_pence:
            wallet.balance_pence = args.topup_pence

        db.session.commit()

        print(f"User:    {user.email} (id={user.id})")
        print(f"Wallet:  £{wallet.balance_pence / 100:.2f}")
        print(f"API key (copy now, shown once):\n\n    {raw_key}\n")
        print("Run the benchmark with:")
        print(f"    export ECOLYXIS_API_KEY={raw_key}")
        print("    python3 benchmark/intelligence_benchmark.py")


if __name__ == "__main__":
    main()
