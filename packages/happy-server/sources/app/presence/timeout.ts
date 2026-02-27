import { db } from "@/storage/db";
import { delay } from "@/utils/delay";
import { forever } from "@/utils/forever";
import { shutdownSignal } from "@/utils/shutdown";
import { buildMachineActivityEphemeral, buildSessionActivityEphemeral, eventRouter } from "@/app/events/eventRouter";
import { log } from "@/utils/log";

// Security: Session timeout configuration
const SESSION_IDLE_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes idle timeout
const SESSION_MAX_DURATION_MS = 8 * 60 * 60 * 1000; // 8 hours max session duration
const MACHINE_IDLE_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes for machines

export function startTimeout() {
    forever('session-timeout', async () => {
        while (true) {
            const now = Date.now();

            // Find idle sessions (no activity for 30 minutes)
            const idleSessions = await db.session.findMany({
                where: {
                    active: true,
                    lastActiveAt: {
                        lte: new Date(now - SESSION_IDLE_TIMEOUT_MS)
                    }
                }
            });

            // Find sessions exceeding max duration (8 hours)
            const maxDurationSessions = await db.session.findMany({
                where: {
                    active: true,
                    createdAt: {
                        lte: new Date(now - SESSION_MAX_DURATION_MS)
                    }
                }
            });

            // Combine and deduplicate
            const sessionsToClose = new Map<string, typeof idleSessions[0]>();
            for (const session of [...idleSessions, ...maxDurationSessions]) {
                if (!sessionsToClose.has(session.id)) {
                    sessionsToClose.set(session.id, session);
                }
            }

            for (const session of sessionsToClose.values()) {
                const isIdleTimeout = session.lastActiveAt.getTime() <= now - SESSION_IDLE_TIMEOUT_MS;
                const reason = isIdleTimeout ? 'idle-timeout' : 'max-duration-reached';

                const updated = await db.session.updateManyAndReturn({
                    where: { id: session.id, active: true },
                    data: {
                        active: false,
                        // Store timeout reason in metadata if possible
                    }
                });

                if (updated.length === 0) {
                    continue;
                }

                log({ module: 'timeout', level: 'info' },
                    `Session ${session.id} closed due to ${reason}`);

                eventRouter.emitEphemeral({
                    userId: session.accountId,
                    payload: buildSessionActivityEphemeral(session.id, false, updated[0].lastActiveAt.getTime(), false),
                    recipientFilter: { type: 'user-scoped-only' }
                });
            }

            // Find timed out machines (keep original 10 min timeout)
            const machines = await db.machine.findMany({
                where: {
                    active: true,
                    lastActiveAt: {
                        lte: new Date(now - MACHINE_IDLE_TIMEOUT_MS)
                    }
                }
            });
            for (const machine of machines) {
                const updated = await db.machine.updateManyAndReturn({
                    where: { id: machine.id, active: true },
                    data: { active: false }
                });
                if (updated.length === 0) {
                    continue;
                }
                eventRouter.emitEphemeral({
                    userId: machine.accountId,
                    payload: buildMachineActivityEphemeral(machine.id, false, updated[0].lastActiveAt.getTime()),
                    recipientFilter: { type: 'user-scoped-only' }
                });
            }

            // Wait for 1 minute
            await delay(1000 * 60, shutdownSignal);
        }
    });
}