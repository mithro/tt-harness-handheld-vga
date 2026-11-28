# SPDX-License-Identifier: Apache-2.0
#
# The firmware, built as a LiteX software package (tt_harness/soc/firmware.py adds it to
# the Builder): make runs in <build>/software/firmware with this file, after LiteX has
# generated the headers (csr.h, soc.h, regions.ld) and built libc and libcompiler_rt.
# demo_json.h and firmware_config.h are generated next to csr.h by firmware.py.
#
# Output: firmware.bin, the ROM image (code and read-only data in the ROM; data and bss
# in main RAM; the stack at the top of the SRAM), firmware.elf and its map.

FIRMWARE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
VGA_MODES_DIR := $(abspath $(FIRMWARE_DIR)/../tt_harness/core)

include ../include/generated/variables.mak
include $(SOC_DIRECTORY)/software/common.mak

OBJECTS = crt0.o main.o hal_litex.o vga_mode.o runner.o demo_parse.o pyrepr.o text.o

CFLAGS += -I$(FIRMWARE_DIR) -I$(BUILDINC_DIRECTORY)/generated -I$(VGA_MODES_DIR) \
          -Wextra -Werror -Wno-missing-field-initializers -Wno-type-limits \
          -Wno-unused-parameter  # LiteX's system.h has unused parameters

all: firmware.bin firmware.size

firmware.elf: $(OBJECTS) $(FIRMWARE_DIR)linker.ld
	$(CC) $(LDFLAGS) -T $(FIRMWARE_DIR)linker.ld -N -o $@ $(OBJECTS) \
		-L../libc -L../libcompiler_rt \
		-Wl,--start-group -lc -lcompiler_rt -Wl,--end-group \
		-Wl,--gc-sections -Wl,-Map,$@.map

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
	chmod -x $@

# The section sizes, for the build log and the size test.
firmware.size: firmware.elf
	$(TARGET_PREFIX)size -A $< > $@

-include $(OBJECTS:.o=.d)

VPATH = $(FIRMWARE_DIR):$(CPU_DIRECTORY)

%.o: %.c
	$(compile)

%.o: %.S
	$(assemble)

clean:
	$(RM) $(OBJECTS) $(OBJECTS:.o=.d) firmware.elf firmware.elf.map firmware.bin firmware.size

.PHONY: all clean
