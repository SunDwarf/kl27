package tf.veriny.kl27.cpu

import org.apache.commons.collections4.queue.CircularFifoQueue
import java.util.*


enum class CPUState {
    halted,
    running,
    errored
}

class CPU(f: K27File) {
    // The current cycle count of the CPU.
    // This is incremented once for every instruction executed.
    var cycleCount: Long = 0L
    // The current file being executed.
    val exeFile = f
    // The current state of the CPU.
    var state = CPUState.halted
    // The current executed memory system.
    val memory = MMU()
    // The registers for this CPU.
    val registers: Array<Register> = Array<Register>(8, { i -> Register(bittiness = 16) })
    // Special registers:
    // The program counter, which is the current address.
    val programCounter = Register(bittiness = 32)
    // The stack.
    val stack: Queue<Int>
    // The last error.
    var lastError: String = ""

    // Mostly used for diagnostics.
    // The recent instruction queue.
    val instructionQueue: Queue<Instruction> = CircularFifoQueue<Instruction>(20)
    // The recent jump queue.
    val recentJumps: Queue<Pair<Int, Int>> = CircularFifoQueue<Pair<Int, Int>>(24);

    init {
        // copy into memory the label table and instructions
        this.exeFile.copyLabelTable(this.memory)
        this.exeFile.copyInstructions(this.memory)

        // set the program counter to the current entry point + offset
        this.programCounter.value = this.exeFile.startOffset + 0x01000
        // create the stack
        this.stack = ArrayDeque<Int>(this.exeFile.stackSize.toInt())
    }

    /**
     * Runs a single cycle of the CPU.
     *
     * Returns the instruction just executed.
     */
    fun runCycle(): Instruction {
        when (this.state) {
            CPUState.halted -> {
                throw RuntimeException("Cannot run cycle on halted CPU")
            }
            CPUState.errored -> {
                throw RuntimeException("Cannot run cycle on errored CPU")
            }
            else -> {
            }
        }

        // increment our cycle count
        this.cycleCount += 1
        // read the next instruction from memory, using the PC value
        val instruction = this.memory.readInstruction(this.programCounter.value)
        this.programCounter.value += 4
        // add to the end of the queue for the main app to poll off of
        this.instructionQueue.add(instruction)

        // MAIN INTERPRETER BLOCK
        // This runs the actual code.
        //println(instruction.opcode.toString())
        when (instruction.opcode.toInt()) {
            0x0 -> {
                // no-op, do nothing
            }
            0x1 -> {
                // JMPL, jump to label
                val offset = this.memory.getLabelOffset(instruction.opcode)
                // we need to set it to 0x01000 + offset
                // otherwise it tries to execute the label table
                val newOffset = 0x01000 + offset
                this.recentJumps.add(Pair(this.programCounter.value, newOffset))
                this.programCounter.value = newOffset
            }
            else -> {
                // unknown opcode
                this.state = CPUState.errored
                this.instructionQueue.add(Instruction(address = this.programCounter.value, opcode = -1, opval = 0))
                this.lastError = "Unknown opcode"
            }
        }

        return instruction

    }
}