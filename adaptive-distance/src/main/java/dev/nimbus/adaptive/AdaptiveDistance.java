package dev.nimbus.adaptive;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.configuration.file.FileConfiguration;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

/**
 * Lowers simulation distance (then view distance) when the server is lagging,
 * and raises them back once it recovers. Simulation distance goes first because
 * it is the expensive one: it controls how many chunks tick mobs, crops and redstone.
 */
public final class AdaptiveDistance extends JavaPlugin {

    private int maxView, minView, maxSim, minSim;
    private double highMspt, lowMspt;
    private int secondsBeforeLowering, secondsBeforeRaising;

    private int overCount, underCount;
    private BukkitTask task;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        FileConfiguration c = getConfig();
        maxView = c.getInt("view-distance.max", 8);
        minView = c.getInt("view-distance.min", 6);
        maxSim = c.getInt("simulation-distance.max", 6);
        minSim = c.getInt("simulation-distance.min", 4);
        highMspt = c.getDouble("lag-mspt", 45.0);
        lowMspt = c.getDouble("healthy-mspt", 30.0);
        secondsBeforeLowering = c.getInt("seconds-before-lowering", 5);
        secondsBeforeRaising = c.getInt("seconds-before-raising", 60);

        for (World w : Bukkit.getWorlds()) {
            w.setViewDistance(maxView);
            w.setSimulationDistance(maxSim);
        }
        task = Bukkit.getScheduler().runTaskTimer(this, this::check, 200L, 20L);
        getLogger().info("Watching tick time: lowering above " + highMspt + " ms, raising below " + lowMspt + " ms.");
    }

    @Override
    public void onDisable() {
        if (task != null) task.cancel();
        for (World w : Bukkit.getWorlds()) {
            w.setViewDistance(maxView);
            w.setSimulationDistance(maxSim);
        }
    }

    private void check() {
        double mspt = Bukkit.getAverageTickTime();
        if (mspt > highMspt) {
            underCount = 0;
            if (++overCount >= secondsBeforeLowering) {
                overCount = 0;
                lower(mspt);
            }
        } else if (mspt < lowMspt) {
            overCount = 0;
            if (++underCount >= secondsBeforeRaising) {
                underCount = 0;
                raise(mspt);
            }
        } else {
            overCount = 0;
            underCount = 0;
        }
    }

    private void lower(double mspt) {
        for (World w : Bukkit.getWorlds()) {
            int sim = w.getSimulationDistance();
            int view = w.getViewDistance();
            if (sim > minSim) {
                w.setSimulationDistance(sim - 1);
                log(w, "lag (" + fmt(mspt) + " ms/tick): simulation distance " + sim + " -> " + (sim - 1));
            } else if (view > minView) {
                w.setViewDistance(view - 1);
                log(w, "lag (" + fmt(mspt) + " ms/tick): view distance " + view + " -> " + (view - 1));
            }
        }
    }

    private void raise(double mspt) {
        for (World w : Bukkit.getWorlds()) {
            int sim = w.getSimulationDistance();
            int view = w.getViewDistance();
            if (view < maxView) {
                w.setViewDistance(view + 1);
                log(w, "recovered (" + fmt(mspt) + " ms/tick): view distance " + view + " -> " + (view + 1));
            } else if (sim < maxSim) {
                w.setSimulationDistance(sim + 1);
                log(w, "recovered (" + fmt(mspt) + " ms/tick): simulation distance " + sim + " -> " + (sim + 1));
            }
        }
    }

    private void log(World w, String msg) {
        getLogger().info("[" + w.getName() + "] " + msg);
    }

    private static String fmt(double d) {
        return String.format("%.1f", d);
    }
}
