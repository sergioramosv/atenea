import argparse
import sys

from .store import Store


def main(argv=None, out=sys.stdout):
    p = argparse.ArgumentParser(prog="inventory")
    p.add_argument("--db", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("sku"); a.add_argument("name"); a.add_argument("qty", type=int); a.add_argument("price", type=float)
    r = sub.add_parser("remove")
    r.add_argument("sku"); r.add_argument("n", type=int)
    sub.add_parser("value")
    lo = sub.add_parser("low")
    lo.add_argument("threshold", type=int)
    args = p.parse_args(argv)

    st = Store(args.db)
    if args.cmd == "add":
        st.add(args.sku, args.name, args.qty, args.price); st.save()
    elif args.cmd == "remove":
        st.remove_stock(args.sku, args.n); st.save()
    elif args.cmd == "value":
        print(f"{st.total_value():.2f}", file=out)
    elif args.cmd == "low":
        for i in st.low_stock(args.threshold):
            print(f"{i.sku}\t{i.name}\t{i.qty}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
