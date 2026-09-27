// uart_peer.c - deterministic UART command/response peer for lager CI.
//
// Target: ESP32-DevKitM-1 (ESP32-U4WDH, single core). Speaks on UART0, the
// port the board's onboard CP2102N exposes, so no extra wiring is needed.
//
// PROTOCOL - line based, LF terminated, 115200 8N1.
//
//   Sent         Replied
//   ----         -------
//   PING         PONG
//   ID?          LAGER-UART-PEER v1
//   ECHO <text>  <text>
//   COUNT?       integer, incremented on every COUNT? since boot
//   RESET        OK  (clears the counter)
//   <anything>   ERR unknown
//
// WHY EVERY COMMAND IS ECHOED FIRST
//
// `lager uart -i` suppresses exactly one inbound line after each line it
// sends (cli/commands/communication/websocket_client.py:145). It does not
// check that the discarded line matches what was sent - it just eats the
// first line back. So this firmware echoes the command (that line is eaten)
// and then sends the reply (that line survives and is what CI asserts on).
//
// For the same reason EVERY command produces exactly two lines, including
// unrecognised ones. A command that replied with nothing would leave the
// CLI's suppression armed, and it would swallow the NEXT command's reply.
//
// There is deliberately no periodic output. Unsolicited traffic would make
// CI assertions racy.

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"

#define UART_PORT   UART_NUM_0
#define RX_BUF_SIZE 1024
#define MAX_LINE    240

static const char *VERSION = "LAGER-UART-PEER v1";
static uint32_t    counter = 0;

static void send_line(const char *s)
{
    uart_write_bytes(UART_PORT, s, strlen(s));
    uart_write_bytes(UART_PORT, "\n", 1);
}

static void handle(char *cmd)
{
    // 1. Echo the command. lager uart eats this line.
    send_line(cmd);

    // 2. The actual reply - the line CI asserts on.
    if (strcmp(cmd, "PING") == 0) {
        send_line("PONG");
    } else if (strcmp(cmd, "ID?") == 0) {
        send_line(VERSION);
    } else if (strcmp(cmd, "COUNT?") == 0) {
        char buf[16];
        snprintf(buf, sizeof(buf), "%lu", (unsigned long)++counter);
        send_line(buf);
    } else if (strcmp(cmd, "RESET") == 0) {
        counter = 0;
        send_line("OK");
    } else if (strncmp(cmd, "ECHO ", 5) == 0) {
        send_line(cmd + 5);
    } else {
        send_line("ERR unknown");
    }
}

void app_main(void)
{
    const uart_config_t cfg = {
        .baud_rate  = 115200,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_driver_install(UART_PORT, RX_BUF_SIZE, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_PORT, &cfg));
    // UART0's default pins are already GPIO1/GPIO3; no uart_set_pin needed.

    vTaskDelay(pdMS_TO_TICKS(200));   // let the ROM boot chatter drain
    send_line(VERSION);               // one banner line, only ever on boot

    char    line[MAX_LINE + 1];
    size_t  len = 0;
    uint8_t ch;

    for (;;) {
        int n = uart_read_bytes(UART_PORT, &ch, 1, pdMS_TO_TICKS(100));
        if (n != 1) {
            continue;
        }

        if (ch == '\n' || ch == '\r') {
            if (len > 0) {
                line[len] = '\0';
                handle(line);
                len = 0;
            }
            // A bare CR/LF (or the LF of a CRLF pair) is ignored rather than
            // answered, so --line-ending crlf does not produce a phantom reply.
            continue;
        }

        if (len < MAX_LINE) {
            line[len++] = (char)ch;
        }
        // Overlong lines are truncated, not split - a split would emit an
        // extra reply pair and desynchronise the caller's line accounting.
    }
}
