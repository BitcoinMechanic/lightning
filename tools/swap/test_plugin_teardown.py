#!/usr/bin/env python3
"""Compile the actual destroy_plugin body with callback-observation stubs.

This tests ordering and callback suppression, not CLN allocation/daemon safety.
The funded process-kill fixture is the separate runtime gate.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#define LD_STATE_RUNNING 0
#define LD_STATE_SHUTDOWN 1
#define AWAITING_GETMANIFEST_RESPONSE 1
#define AWAITING_INIT_RESPONSE 2
#define INIT_COMPLETE 3
struct jsonrpc_request { int unused; } request;
struct list { int unused; };
struct lightningd { int state; void *log; };
struct plugins { struct lightningd *ld; void *subscriptions; struct list plugins; };
struct plugin {
    struct list list;
    void *stdout_conn, *pending_requests, *custom_msgs, *log;
    char *subscriptions;
    struct plugins *plugins;
    int plugin_state;
    bool important;
};
static struct jsonrpc_request *requests[2];
static int replies, manifests, initted, stopped, exits, breaks, freed, custom;
static void list_del(struct list *l) { (void)l; }
static void io_set_finish(void *c, void *f, void *a) { (void)c; (void)f; (void)a; }
#define tal_arr(ctx, type, count) requests
static size_t tal_count(const void *p) { return p == requests ? 2 : 0; }
static void tal_free(void *p) { assert(p == requests); freed += 2; }
static bool request_add(const char *id, struct jsonrpc_request *r,
                        struct jsonrpc_request ***rs) { (void)id; (void)r; (void)rs; return true; }
static void strmap_iterate(void *map, bool (*cb)(const char *, struct jsonrpc_request *,
                           struct jsonrpc_request ***), struct jsonrpc_request ***r)
{ (void)map; (void)cb; (void)r; requests[0] = requests[1] = &request; }
static void plugin_terminated_fail_req(struct plugin *p, struct jsonrpc_request *r)
{ (void)p; (void)r; replies++; }
static void plugin_subscription_htable_del(void *t, void *s) { (void)t; (void)s; }
static void check_plugins_manifests(struct plugins *p, void *log) { (void)p; (void)log; manifests++; }
static bool list_empty(struct list *l) { (void)l; return true; }
static void log_debug(void *l, const char *f, const char *name) { (void)l; (void)f; (void)name; }
static void log_broken(void *l, const char *f) { (void)l; (void)f; }
static void io_break(void (*f)(struct plugin *)) { (void)f; breaks++; }
static void check_plugins_initted(struct plugins *p) { (void)p; initted++; }
static void lightningd_exit(struct lightningd *l, int code) { (void)l; assert(code == 1); exits++; }
static void tell_connectd_custommsgs(struct plugins *p) { (void)p; custom++; }
static void notify_plugin_stopped(struct lightningd *l, struct plugin *p) { (void)l; (void)p; stopped++; }
'''

MAIN = r'''
int main(void) {
    for (int state = 0; state <= 1; state++) {
        for (int important = 0; important <= 1; important++) {
            for (int phase = 1; phase <= 3; phase++) {
                struct lightningd ld = { .state = state };
                struct plugins plugins = { .ld = &ld };
                struct plugin plugin = { .plugins = &plugins, .important = important,
                                         .plugin_state = phase, .stdout_conn = &ld };
                replies = manifests = initted = stopped = exits = breaks = freed = custom = 0;
                destroy_plugin(&plugin);
                assert(freed == 2);
                if (state == LD_STATE_SHUTDOWN) {
                    assert(replies == 0 && manifests == 0 && initted == 0);
                    assert(exits == 0 && breaks == 1 && stopped == 0);
                } else if (important) {
                    assert(replies == 0 && manifests == 0 && initted == 0);
                    assert(exits == 1 && breaks == 0 && stopped == 0);
                } else {
                    assert(replies == 2 && exits == 0 && breaks == 0 && stopped == 1);
                    assert(manifests == (phase == AWAITING_GETMANIFEST_RESPONSE));
                    assert(initted == (phase == AWAITING_INIT_RESPONSE));
                }
            }
        }
    }
    puts("PASS: actual C teardown body; 12 importance/startup/shutdown cases, two pending requests each");
    return 0;
}
'''


def run(source):
    text = source.read_text()
    start = text.index('static void destroy_plugin(struct plugin *p)\n{')
    end = text.index('\nstatic u32 file_checksum',start)
    with tempfile.TemporaryDirectory(prefix='swap-teardown-0070-') as tmp:
        root = Path(tmp)
        (root/'test.c').write_text(PRELUDE+text[start:end]+MAIN)
        subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror',
                        '-Wno-unused-function',str(root/'test.c'),'-o',str(root/'test')],check=True)
        subprocess.run([str(root/'test')],check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[2]/'lightningd/plugin.c')
    run(parser.parse_args().source)
