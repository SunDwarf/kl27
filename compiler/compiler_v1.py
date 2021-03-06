#!/usr/bin/env python3
"""
The KL27 source code compiler.
Unlike the reference implementation, this is implemented in Python 3.6.

This compiler does several things:

 - Parses the KLT file line by line
 - Generates a label table
 - Parses and creates each instruction
 - Inserts label addresses
"""
import argparse
import os
import pprint
import shlex
import struct
import sys
import zlib

from collections import OrderedDict


class LabelPlaceholder:
    def __init__(self, label_name: str):
        self.label_name = label_name

    def resolve(self, table: dict):
        """
        Resolves the jump address for this label.
        """
        return table[self.label_name][0].to_bytes(2, byteorder="big")

    # used for moving the pointer ahead
    def __len__(self):
        return 2


# register mapping
R_MAP = {
    "MAR": 8,
    "MVR": 9,
    "PC": 10,
    **{"R{}".format(v): v for v in range(0, 8)}
}


# function definitions
# all functions take one arg, the line
# and returns an iterable of bytestrings or similar
def compile_nop(line: str):
    # fmt: `nop`
    return [b"\x00\x00\x00\x00"]


def compile_hlt(line: str):
    # fmt: `hlt`
    # halts the CPU
    return [b"\x00\x01\x00\x00"]


# stack operations
def compile_sl(line: str):
    # fmt: `sl <int>`
    # puts a literal on the stack
    val = int(line, 0)

    if val > 0xffff:
        # figure out how many times we need to multiply it by 32767
        # then ADD the remainder
        mul_amount = val // 0x7fff
        remainder = val % 0x7fff
        return [
            b"\x00\x02", mul_amount.to_bytes(2, byteorder="big"),
            # stack load the amount to multiply
            b"\x00\x02", b"\x7f\xff",  # stack load 32767
            b"\x00\x31", b"\x00\x00",  # call the multiplication op
            b"\x00\x02", remainder.to_bytes(2, byteorder="big"),  # load remainder
            b"\x00\x30", b"\x00\x00"  # call the addition op
        ]

    return [b"\x00\x02", val.to_bytes(2, byteorder="big")]


def compile_spop(line: str):
    # fmt: `spop <i>`
    # pops the top <x> items from the stack
    if line:
        val = int(line, 0)
    else:
        val = 1

    return [b"\x00\x03", val.to_bytes(2, byteorder="big")]


def compile_llbl(line: str):
    # fmt: `llbl <label>`
    # loads the address of a label onto the stack
    pl = LabelPlaceholder(line)

    return [
        b"\x00\x04", pl
    ]


# register operations
def compile_rgw(line: str):
    # fmt: `rgw <reg>`
    # pops the top item from the stack, and writes it to the register
    reg = line.upper()

    return [b"\x00\x10", R_MAP[reg].to_bytes(2, byteorder="big")]


def compile_rgr(line: str):
    # fmt: `rgr <reg>`
    # reads the value from the register and puts it on the stack
    reg = line.upper()

    return [b"\x00\x11", R_MAP[reg].to_bytes(2, byteorder="big")]


def compile_mmr(line: str):
    # fmt: `mmr`
    # reads from memory into the MVR with the address specified by the MAR

    # the 2nd arg is the number of bytes to read
    if line:
        val = int(line, 0)
    else:
        val = 4

    return [b"\x00\x12", val.to_bytes(2, byteorder="big")]


def compile_mmw(line: str):
    # fmt: `mmw`
    # writes from memory from the MVR to memory with the address specified by the MAR

    # the 2nd arg is the number of bytes to write
    if line:
        val = int(line, 0)
    else:
        val = 4

    return [b"\x00\x13", val.to_bytes(2, byteorder="big")]


# jump operations
def compile_jmpl(line: str):
    # fmt: `jmpl <label>`
    # JuMP Label. This will jump to the specified label.
    # this is NOT a real instruction!
    # it compiles to `llbl <label>; jmpa`.
    # it is not recommended to use this; use `jmpl` instead.

    pl = LabelPlaceholder(line)

    code = [
        b"\x00\x04", pl,  # llbl label
        b"\x00\x23", b"\x00\x00"  # jump absolute
    ]

    return code


def compile_jmpr(line: str):
    # fmt: `jmpr <label>`
    # this will place the current memory location at 4 * R7, increase R7, then jump to the label

    # get a label placeholder that we need to jump to label
    pl = LabelPlaceholder(line)

    code = [
        b"\x00\x20", pl
    ]

    return code


def compile_ret(line: str):
    # fmt: `ret`
    # RETurn from jump
    # This will jump to the address specified in the jump stack by the pointer in R7.

    code = [
        b"\x00\x21\x00\x00"
    ]

    return code


def compile_jmpa(line: str):
    # fmt: `jmpa`
    # JuMP Absolute. This will jump to the absolute address, specified by TOS.
    # It is very rare that this is needed explicitly; a JMPL or JMPR will often be better.
    return [b"\x00\x23\x00\x00"]


# math operations
def compile_add(line: str):
    # fmt: `add [val]`
    # if val is not specified, it will load from the stack
    ins = []
    if line:
        val = int(line, 0).to_bytes(length=2, byteorder="big")
        ins.extend([b"\x00\x02", val])

    return ins + [b"\x00\x30\x00\x00"]


# im not 100% mean
# so I do actually allow a multiplication op
# rather than forcing multiple adds
def compile_mul(line: str):
    # fmt: `mul [val]`
    # multiples TOS by the value provided
    # if no value is provided, it will use TOS
    instruction = []

    if line:
        val = int(line, 0).to_bytes(length=2, byteorder="big")
        instruction.extend([b"\x00\x02", val])

    instruction.extend([b"\x00\x31\x00\x00"])
    return instruction


def compile_sub(line: str):
    # fmt: `sub [val]`
    # multiplies TOS by the value provided
    # if no value is provided, it will use TOS
    instruction = []

    if line:
        val = int(line, 0).to_bytes(length=2, byteorder="big")
        instruction.extend([b"\x00\x02", val])

    instruction.extend([b"\x00\x032\x00\x00"])
    return instruction


def kl27_compile(args: argparse.Namespace):
    print("compiling", args.infile)

    with open(args.infile) as f:
        data = f.read()

    # current offset
    current_pointer = 0
    # label to address mapping
    label_table = OrderedDict()
    # machine code memory
    code = []
    # current includes table
    # prevents re-including files
    includes = []

    # current label
    current_label = None

    lines = data.splitlines()
    lineno = 0

    # preprocessor checks
    def process_include(line: str):
        # this is the file we want to include
        second = shlex.split(line)[1]
        if not os.path.exists(second):
            print(f"error: no such file: {second}")
            sys.exit(1)

        with open(second) as f:
            firstline = f.readline()
            firstline = firstline.replace("\n", "").replace("\r", "")
            if not firstline[0:3] == "#ID":
                print(f"warning: included file '{second}' does not have an ID directive.\n"
                      f"\tThis file could potentially be included multiple times.")
                f.seek(0)
            else:
                id = " ".join(firstline.split(" ")[1:])
                if id in includes:
                    # don't re-include
                    print(f"not re-including file '{second}")
                    return

                includes.append(id)
                print(f"including file '{second}' ")

            newlines = f.read()

        # insert the new lines into the `lines` count
        # this is a hacky slice assignment
        lines[lineno:lineno] = newlines.splitlines()

    while True:
        # load the next line from splitlines
        try:
            line = lines[lineno]
        except IndexError:
            break
        else:
            lineno += 1

        # clean up shit whitespace
        line = line.lstrip().rstrip()
        # ignore whitespace
        if not line:
            continue

        # strip comments
        if line.startswith("//"):
            continue

        # preprocessor check
        if line.startswith("#"):
            processor = line.split(" ")[0][1:]
            func = locals().get(f"process_{processor}")
            if not func:
                print(f"error: line {lineno}: unknown statement `{processor}`")
                return 1
            else:
                # call the preprocessor
                func(line)
                continue

        # check if it's a label
        if line.endswith(":"):
            # save the address of this label
            current_label = line[:-1]
            if current_label in label_table:
                print(f"warning: redefined label {current_label}, old code is unreachable")

            label_table[current_label] = (len(label_table), current_pointer)
            print(f"\ncompiling label {current_label} at address {current_pointer}")
            # don't increment the pointer, labels don't have pointers
            continue

        if current_label is None:
            if args.no_automatic_main:
                print(f"error: line {lineno}: no label specified.")
                return 1
            print("warning: no label specified, assuming main")
            print("(pass --no-automatic-main to disable this)")
            current_label = "main"
            label_table[current_label] = (len(label_table), current_pointer)

        instruction = line.split(" ")[0]

        # extract the function to compile the instruction
        glob = globals()
        func = f"compile_{instruction.lower()}"
        if func not in glob:
            print(f"error: line {lineno}: in label {current_label}:\n\t unknown instruction "
                  f"`{instruction}`.")
            return 1
        print(f"compiling instruction {instruction} at address {hex(current_pointer)} inside "
              f"{current_label}")

        f = glob[func]
        # call with the rest of the line to parse and construct
        instructions = f(" ".join(line.split(" ")[1:]))
        if instructions:
            code.extend(instructions)

        # increment the pointer by the length of instructions produced
        to_incr = sum(len(x) for x in instructions)
        current_pointer += to_incr

    print("\nlabel table:")
    pprint.pprint(label_table)

    if args.entry_point not in label_table:
        print("error: could not find entry point label")
        return 1

    entry: int = label_table[args.entry_point][1]
    print(f"`{args.entry_point}` entry point address:", entry)

    # generate the header

    print("\ngenerating label table...")
    final_label_table = [
        len(label_table).to_bytes(length=2, byteorder="big")
    ]

    for id, addr in label_table.values():
        # pack the (id, addr)
        final_label_table.append(
            struct.pack(">i", addr)
        )

    final_label_table.append(b"\xff\xff\xff\xff")
    print("generated", len(final_label_table), "table entries")

    final_label_table = b"".join(final_label_table)

    resolved_labels = set()

    def fix_jumps():
        loop_code = code.copy()

        for n, i in enumerate(loop_code):
            if isinstance(i, LabelPlaceholder):
                if i.label_name not in label_table:
                    print(f"error: unknown label {i.label_name} to resolve")
                    sys.exit(1)

                print(f"resolving jump for `{i.label_name}` to "
                      f"`{hex(label_table[i.label_name][1])}`")
                # replace it with the resolved
                code[n] = i.resolve(label_table)
                resolved_labels.add(i.label_name)

    print("\nfixing jumps...")
    fix_jumps()

    print()
    # check to see if any labels were unused
    for label in label_table:
        if label == args.entry_point:
            continue

        if label not in resolved_labels:
            print(f"warning: unused label `{label}`")

    final_code = b"".join(code)

    print("instructions parsed (est.):", len(final_code) // 4)

    print("\ngenerating header...")
    header = []
    # 1: magic number
    header += [b"KL27"]
    # 2: K_VERSION, which is 1
    header += [b"\x01"]
    # 3: K_COMPRESS
    header += [b"\x00"]
    # 4: K_BODY, the main entry point
    header += [entry.to_bytes(4, byteorder="big")]
    # 5: K_STACKSIZE
    header += [(4).to_bytes(2, byteorder="big")]
    # 6: K_CHECKSUM
    header += [zlib.crc32(final_code).to_bytes(4, byteorder="big")]
    header = b"".join(header)

    with open(args.outfile, 'wb') as out:
        final = out.write(header)
        final += out.write(final_label_table)
        final += out.write(final_code)

    print(f"compiled file successfully! written {final} bytes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KL27 basic compiler")
    parser.add_argument("-i", "--infile", help="The input file to parse.")
    parser.add_argument("-o", "--outfile", help="The output file to produce.")

    parser.add_argument_group("Compiler options")
    parser.add_argument("--entry-point", default="main", help="The entry point to use")
    parser.add_argument("--no-automatic-main", action="store_true")

    args = parser.parse_args()

    sys.exit(kl27_compile(args))
