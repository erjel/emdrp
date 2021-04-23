#snakemake -j --use-conda # local execution
#snakemake --profile axon -j --use-conda # slurm exectution on axon

configfile: "config.yml"
root = config['local_data_path']

from pathlib import Path

rule all:
    input:
        expand( root + '/data_out/tutorial_ECS/xfold/{ident}_probs.h5', 
            ident=['M0007', 'M0027']),

rule merge_predicted_probabilities:
    output:
        root + '/data_out/tutorial_ECS/xfold/{ident}_probs.h5',
    input:
        expand(root + '/data_out/tutorial_ECS/xfold/{{ident}}_{replicate}.0_probs.h5',
            replicate = glob_wildcards(root + '/data_out/tutorial_ECS/xfold/{{ident}}_{replicate}.0_probs.h5').replicate),
    params:
        src_path = root + '/data_out/tutorial_ECS/xfold/',
        srcfiles = lambda wc: [p.name for p in Path(root + '/data_out/tutorial_ECS/xfold/').glob(f'{wc.ident}_*.0_probs.h5')],
        dim_order = lambda wc: ['xyz' for p in Path(root + '/data_out/tutorial_ECS/xfold/').glob(f'{wc.ident}_*.0_probs.h5')], 
    resources:
        time='00:05:00', 
        partition="p.default",
        #gres="gpu:rtx2080ti:1",
        mem="32000",
    conda:
        'environment.yml'
    shell:
        'python -u recon/emdrp/dpMergeProbs.py' +
        ' --srcpath {params.src_path}'
        ' --srcfiles {params.srcfiles}'
        ' --dim-orderings {params.dim_order}'
        ' --outprobs {output}'
        ' --chunk 18 15 3 --size 128 128 128 --types ICS --ops mean min --dpM'