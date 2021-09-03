import os
import jax
import jax.numpy as jnp

from urllib import request
from concurrent import futures
import pickle

from alphafold.data.tools import jackhmmer
from alphafold.data import parsers
from alphafold.data import pipeline
from alphafold.common import protein

import numpy as np

import re
import colabfold as cf

try:
  from google.colab import files
  IN_COLAB = True
except:
  IN_COLAB = False

import tqdm.notebook
TQDM_BAR_FORMAT = '{l_bar}{bar}| {n_fmt}/{total_fmt} [elapsed: {elapsed} remaining: {remaining}]'

def run_jackhmmer(sequence, prefix, jackhmmer_binary_path='jackhmmer'):

  fasta_path = f"{prefix}.fasta"
  with open(fasta_path, 'wt') as f:
    f.write(f'>query\n{sequence}')

  pickled_msa_path = f"{prefix}.jackhmmer.pickle"
  if os.path.isfile(pickled_msa_path):
    msas_dict = pickle.load(open(pickled_msa_path,"rb"))
    msas, deletion_matrices, names = (msas_dict[k] for k in ['msas', 'deletion_matrices', 'names'])
    full_msa = []
    for msa in msas:
      full_msa += msa
  else:
    # --- Find the closest source ---
    test_url_pattern = 'https://storage.googleapis.com/alphafold-colab{:s}/latest/uniref90_2021_03.fasta.1'
    ex = futures.ThreadPoolExecutor(3)
    def fetch(source):
      request.urlretrieve(test_url_pattern.format(source))
      return source
    fs = [ex.submit(fetch, source) for source in ['', '-europe', '-asia']]
    source = None
    for f in futures.as_completed(fs):
      source = f.result()
      ex.shutdown()
      break
      
    dbs = []

    num_jackhmmer_chunks = {'uniref90': 59, 'smallbfd': 17, 'mgnify': 71}
    total_jackhmmer_chunks = sum(num_jackhmmer_chunks.values())
    with tqdm.notebook.tqdm(total=total_jackhmmer_chunks, bar_format=TQDM_BAR_FORMAT) as pbar:
      def jackhmmer_chunk_callback(i):
        pbar.update(n=1)

      pbar.set_description('Searching uniref90')
      jackhmmer_uniref90_runner = jackhmmer.Jackhmmer(
          binary_path=jackhmmer_binary_path,
          database_path=f'https://storage.googleapis.com/alphafold-colab{source}/latest/uniref90_2021_03.fasta',
          get_tblout=True,
          num_streamed_chunks=num_jackhmmer_chunks['uniref90'],
          streaming_callback=jackhmmer_chunk_callback,
          z_value=135301051)
      dbs.append(('uniref90', jackhmmer_uniref90_runner.query(fasta_path)))

      pbar.set_description('Searching smallbfd')
      jackhmmer_smallbfd_runner = jackhmmer.Jackhmmer(
          binary_path=jackhmmer_binary_path,
          database_path=f'https://storage.googleapis.com/alphafold-colab{source}/latest/bfd-first_non_consensus_sequences.fasta',
          get_tblout=True,
          num_streamed_chunks=num_jackhmmer_chunks['smallbfd'],
          streaming_callback=jackhmmer_chunk_callback,
          z_value=65984053)
      dbs.append(('smallbfd', jackhmmer_smallbfd_runner.query(fasta_path)))

      pbar.set_description('Searching mgnify')
      jackhmmer_mgnify_runner = jackhmmer.Jackhmmer(
          binary_path=jackhmmer_binary_path,
          database_path=f'https://storage.googleapis.com/alphafold-colab{source}/latest/mgy_clusters_2019_05.fasta',
          get_tblout=True,
          num_streamed_chunks=num_jackhmmer_chunks['mgnify'],
          streaming_callback=jackhmmer_chunk_callback,
          z_value=304820129)
      dbs.append(('mgnify', jackhmmer_mgnify_runner.query(fasta_path)))

    # --- Extract the MSAs and visualize ---
    # Extract the MSAs from the Stockholm files.
    # NB: deduplication happens later in pipeline.make_msa_features.

    mgnify_max_hits = 501
    msas = []
    deletion_matrices = []
    names = []
    for db_name, db_results in dbs:
      unsorted_results = []
      for i, result in enumerate(db_results):
        msa, deletion_matrix, target_names = parsers.parse_stockholm(result['sto'])
        e_values_dict = parsers.parse_e_values_from_tblout(result['tbl'])
        e_values = [e_values_dict[t.split('/')[0]] for t in target_names]
        zipped_results = zip(msa, deletion_matrix, target_names, e_values)
        if i != 0:
          # Only take query from the first chunk
          zipped_results = [x for x in zipped_results if x[2] != 'query']
        unsorted_results.extend(zipped_results)
      sorted_by_evalue = sorted(unsorted_results, key=lambda x: x[3])
      db_msas, db_deletion_matrices, db_names, _ = zip(*sorted_by_evalue)
      if db_msas:
        if db_name == 'mgnify':
          db_msas = db_msas[:mgnify_max_hits]
          db_deletion_matrices = db_deletion_matrices[:mgnify_max_hits]
          db_names = db_names[:mgnify_max_hits]
        msas.append(db_msas)
        deletion_matrices.append(db_deletion_matrices)
        names.append(db_names)
        msa_size = len(set(db_msas))
        print(f'{msa_size} Sequences Found in {db_name}')

      pickle.dump({"msas":msas,
                   "deletion_matrices":deletion_matrices,
                   "names":names}, open(pickled_msa_path,"wb"))
  return msas, deletion_matrices, names

def prep_inputs(sequence, jobname, homooligomer, clean=True, verbose=False):
  # process inputs
  sequence = re.sub("[^A-Z:/]", "", sequence.upper())
  sequence = re.sub(":+",":",sequence)
  sequence = re.sub("/+","/",sequence)
  sequence = re.sub("^[:/]+","",sequence)
  sequence = re.sub("[:/]+$","",sequence)
  jobname = re.sub(r'\W+', '', jobname)
  homooligomer = re.sub("[:/]+",":",homooligomer)
  homooligomer = re.sub("^[:/]+","",homooligomer)
  homooligomer = re.sub("[:/]+$","",homooligomer)

  if len(homooligomer) == 0: homooligomer = "1"
  homooligomer = re.sub("[^0-9:]", "", homooligomer)

  # define inputs
  I = {"ori_sequence":sequence,
      "sequence":sequence.replace("/","").replace(":",""),
      "seqs":sequence.replace("/","").split(":"),
      "homooligomer":homooligomer,
      "homooligomers":[int(h) for h in homooligomer.split(":")],
      "msas":[], "deletion_matrices":[]}

  # adjust homooligomer option
  if len(I["seqs"]) != len(I["homooligomers"]):
    if len(I["homooligomers"]) == 1:
      I["homooligomers"] = [I["homooligomers"][0]] * len(I["seqs"])
    else:
      if verbose:
        print("WARNING: Mismatch between number of breaks ':' in 'sequence' and 'homooligomer' definition")
      while len(I["seqs"]) > len(I["homooligomers"]):
        I["homooligomers"].append(1)
      I["homooligomers"] = I["homooligomers"][:len(I["seqs"])]
    I["homooligomer"] = ":".join([str(h) for h in I["homooligomers"]])

  # define full sequence being modelled
  I["full_sequence"] = ''.join([s*h for s,h in zip(I["seqs"],I["homooligomers"])])
  I["lengths"] = [len(seq) for seq in I["seqs"]]

  # prediction directory
  I["output_dir"] = 'prediction_' + jobname + '_' + cf.get_hash(I["full_sequence"])[:5]
  os.makedirs(I["output_dir"], exist_ok=True)

  # delete existing files in working directory
  if clean:
    for f in os.listdir(I["output_dir"]):
      os.remove(os.path.join(I["output_dir"], f))

  if verbose and len(I["full_sequence"]) > 1400:
    print(f"WARNING: For a typical Google-Colab-GPU (16G) session, the max total length is ~1400 residues. You are at {len(full_sequence)}!")
    print(f"Run Alphafold may crash, unless you trim to the protein(s) to a short length. (See trim options below).")

  if verbose:
    print(f"homooligomer: {I['homooligomer']}")
    print(f"total_length: {len(I['full_sequence'])}")
    print(f"working_directory: {I['output_dir']}")
    
  return I

def prep_msa(I, msa_method, add_custom_msa, msa_format, pair_mode, pair_cov, pair_qid, verbose=False, TMP_DIR="tmp"):

  # clear previous inputs
  I["msas"] = []
  I["deletion_matrices"] = []

  if add_custom_msa:
    print(f"upload custom msa in '{msa_format}' format")
    msa_dict = files.upload()
    lines = msa_dict[list(msa_dict.keys())[0]].decode()
    
    # convert to a3m
    input_file = os.path.join(TMP_DIR,f"upload.{msa_format}")
    output_file = os.path.join(TMP_DIR,f"upload.a3m")
    with open(input_file,"w") as tmp_upload:
      tmp_upload.write(lines)
    os.system(f"reformat.pl {msa_format} a3m {input_file} {output_file}")
    a3m_lines = open(output_file,"r").read()

    # parse
    msa, mtx = parsers.parse_a3m(a3m_lines)
    I["msas"].append(msa)
    I["deletion_matrices"].append(mtx)
    if len(I["msas"][0][0]) != len(I["sequence"]):
      raise ValueError("ERROR: the length of msa does not match input sequence")

  if msa_method == "precomputed":
    print("upload precomputed pickled msa from previous run")
    uploaded_dict = files.upload()
    uploaded_filename = list(uploaded_dict.keys())[0]
    uploaded_content = pickle.loads(uploaded_dict[uploaded_filename])
    I.update(uploaded_content)

  elif msa_method == "single_sequence":
    if len(I["msas"]) == 0:
      I["msas"].append([I["sequence"]])
      I["deletion_matrices"].append([[0]*len(I["sequence"])])

  else:
    _blank_seq = ["-" * L for L in I["lengths"]]
    _blank_mtx = [[0] * L for L in I["lengths"]]
    def _pad(ns,vals,mode):
      if mode == "seq": _blank = _blank_seq.copy()
      if mode == "mtx": _blank = _blank_mtx.copy()
      if isinstance(ns, list):
        for n,val in zip(ns,vals): _blank[n] = val
      else: _blank[ns] = vals
      if mode == "seq": return "".join(_blank)
      if mode == "mtx": return sum(_blank,[])

    if len(I["seqs"]) == 1 or "unpaired" in pair_mode:
      # gather msas
      if msa_method == "mmseqs2":
        prefix = cf.get_hash(I["sequence"])
        prefix = os.path.join(TMP_DIR,prefix)
        print(f"running mmseqs2")
        A3M_LINES = cf.run_mmseqs2(I["seqs"], prefix, filter=True)

      for n, seq in enumerate(I["seqs"]):
        # tmp directory
        prefix = cf.get_hash(seq)
        prefix = os.path.join(TMP_DIR,prefix)

        if msa_method == "mmseqs2":
          # run mmseqs2
          a3m_lines = A3M_LINES[n]
          msa, mtx = parsers.parse_a3m(a3m_lines)
          msas_, mtxs_ = [msa],[mtx]

        elif msa_method == "jackhmmer":
          print(f"running jackhmmer on seq_{n}")
          # run jackhmmer
          msas_, mtxs_, names_ = ([sum(x,())] for x in run_jackhmmer(seq, prefix))
        
        # pad sequences
        for msa_,mtx_ in zip(msas_,mtxs_):
          msa,mtx = [I["sequence"]],[[0]*len(I["sequence"])]      
          for s,m in zip(msa_,mtx_):
            msa.append(_pad(n,s,"seq"))
            mtx.append(_pad(n,m,"mtx"))

          I["msas"].append(msa)
          I["deletion_matrices"].append(mtx)

    ####################################################################################
    # PAIR_MSA
    ####################################################################################
    if len(I["seqs"]) > 1 and (pair_mode == "paired" or pair_mode == "unpaired+paired"):
      print("attempting to pair some sequences...")

      if msa_method == "mmseqs2":
        prefix = cf.get_hash(I["sequence"])
        prefix = os.path.join(TMP_DIR,prefix)
        print(f"running mmseqs2_noenv_nofilter on all seqs")
        A3M_LINES = cf.run_mmseqs2(I["seqs"], prefix, use_env=False, use_filter=False)

      _data = []
      for a in range(len(I["seqs"])):
        print(f"prepping seq_{a}")
        _seq = I["seqs"][a]
        _prefix = os.path.join(TMP_DIR,cf.get_hash(_seq))

        if msa_method == "mmseqs2":
          a3m_lines = A3M_LINES[a]
          _msa, _mtx, _lab = pairmsa.parse_a3m(a3m_lines,
                                              filter_qid=pair_qid/100,
                                              filter_cov=pair_cov/100)

        elif msa_method == "jackhmmer":
          _msas, _mtxs, _names = run_jackhmmer(_seq, _prefix)
          _msa, _mtx, _lab = pairmsa.get_uni_jackhmmer(_msas[0], _mtxs[0], _names[0],
                                                      filter_qid=pair_qid/100,
                                                      filter_cov=pair_cov/100)

        if len(_msa) > 1:
          _data.append(pairmsa.hash_it(_msa, _lab, _mtx, call_uniprot=False))
        else:
          _data.append(None)
      
      Ln = len(I["seqs"])
      O = [[None for _ in I["seqs"]] for _ in I["seqs"]]
      for a in range(Ln):
        if _data[a] is not None:
          for b in range(a+1,Ln):
            if _data[b] is not None:
              print(f"attempting pairwise stitch for {a} {b}")            
              O[a][b] = pairmsa._stitch(_data[a],_data[b])
              _seq_a, _seq_b, _mtx_a, _mtx_b = (*O[a][b]["seq"],*O[a][b]["mtx"])

              ##############################################
              # filter to remove redundant sequences
              ##############################################
              ok = []
              with open("tmp/tmp.fas","w") as fas_file:
                fas_file.writelines([f">{n}\n{a+b}\n" for n,(a,b) in enumerate(zip(_seq_a,_seq_b))])
              os.system("hhfilter -maxseq 1000000 -i tmp/tmp.fas -o tmp/tmp.id90.fas -id 90")
              for line in open("tmp/tmp.id90.fas","r"):
                if line.startswith(">"): ok.append(int(line[1:]))
              ##############################################      
              if verbose:      
                print(f"found {len(_seq_a)} pairs ({len(ok)} after filtering)")

              if len(_seq_a) > 0:
                msa,mtx = [I["sequence"]],[[0]*len(I["sequence"])]
                for s_a,s_b,m_a,m_b in zip(_seq_a, _seq_b, _mtx_a, _mtx_b):
                  msa.append(_pad([a,b],[s_a,s_b],"seq"))
                  mtx.append(_pad([a,b],[m_a,m_b],"mtx"))
                I["msas"].append(msa)
                I["deletion_matrices"].append(mtx)
      
  ####################################################################################

  # save MSA as pickle
  pickle.dump({"msas":I["msas"],"deletion_matrices":I["deletion_matrices"]},
              open(os.path.join(I["output_dir"],"msa.pickle"),"wb"))
  return I

def prep_filter(I, trim, trim_inverse=False, cov=0, qid=0, verbose=False):
  trim = re.sub("[^0-9A-Z,-]", "", trim.upper())
  trim = re.sub(",+",",",trim)
  trim = re.sub("^[,]+","",trim)
  trim = re.sub("[,]+$","",trim)
  if trim != "" or cov > 0 or qid > 0:
    mod_I = dict(I)
    
    if trim != "":
      mod_I.update(cf.trim_inputs(trim, mod_I["msas"], mod_I["deletion_matrices"],
                                  mod_I["ori_sequence"], inverse=trim_inverse))
      
      mod_I["homooligomers"] = [mod_I["homooligomers"][c] for c in mod_I["chains"]]
      mod_I["sequence"] = mod_I["ori_sequence"].replace("/","").replace(":","")
      mod_I["seqs"] = mod_I["ori_sequence"].replace("/","").split(":") 
      mod_I["full_sequence"] = "".join([s*h for s,h in zip(mod_I["seqs"], mod_I["homooligomers"])])
      new_length = len(mod_I["full_sequence"])
      if verbose:
        print(f"total_length: '{new_length}' after trimming")

    if cov > 0 or qid > 0:
      mod_I.update(cf.cov_qid_filter(mod_I["msas"], mod_I["deletion_matrices"],
                                    mod_I["ori_sequence"],
                                    cov=cov/100, qid=qid/100))
    return mod_I
  else:
    return I  

  
##########################################################################################
def _placeholder_template_feats(num_templates_, num_res_):
  return {
      'template_aatype': np.zeros([num_templates_, num_res_, 22], np.float32),
      'template_all_atom_masks': np.zeros([num_templates_, num_res_, 37, 3], np.float32),
      'template_all_atom_positions': np.zeros([num_templates_, num_res_, 37], np.float32),
      'template_domain_names': np.zeros([num_templates_], np.float32),
      'template_sum_probs': np.zeros([num_templates_], np.float32),
  }

def do_subsample_msa(F, random_seed=0):
  '''subsample msa to avoid running out of memory'''
  N = len(F["msa"])
  L = len(F["residue_index"])
  N_ = int(3E7/L)
  if N > N_:
    print(f"whhhaaa... too many sequences ({N}) subsampling to {N_}")
    np.random.seed(random_seed)
    idx = np.append(0,np.random.permutation(np.arange(1,N)))[:N_]
    F_ = {}
    F_["msa"] = F["msa"][idx]
    F_["deletion_matrix_int"] = F["deletion_matrix_int"][idx]
    F_["num_alignments"] = np.full_like(F["num_alignments"],N_)
    for k in F.keys():
      if k not in F_: F_[k] = F[k]      
    return F_
  else:
    return F

def parse_results(prediction_result, processed_feature_dict):
  '''parse results and convert to numpy arrays'''
  to_np = lambda a: np.asarray(a)
  def class_to_np(c):
    class dict2obj():
      def __init__(self, d):
        for k,v in d.items(): setattr(self, k, to_np(v))
    return dict2obj(c.__dict__)

  b_factors = prediction_result['plddt'][:,None] * prediction_result['structure_module']['final_atom_mask']  
  dist_bins = jax.numpy.append(0,prediction_result["distogram"]["bin_edges"])
  dist_mtx = dist_bins[prediction_result["distogram"]["logits"].argmax(-1)]
  contact_mtx = jax.nn.softmax(prediction_result["distogram"]["logits"])[:,:,dist_bins < 8].sum(-1)
  p = protein.from_prediction(processed_feature_dict, prediction_result, b_factors=b_factors)  
  out = {"unrelaxed_protein": class_to_np(p),
         "plddt": to_np(prediction_result['plddt']),
         "pLDDT": to_np(prediction_result['plddt'].mean()),
         "dists": to_np(dist_mtx),
         "adj": to_np(contact_mtx)}
  if "ptm" in prediction_result:
    out["pae"] = to_np(prediction_result['predicted_aligned_error'])
    out["pTMscore"] = to_np(prediction_result['ptm'])
  return out

def do_report(I, outs, key, show_images=True):
  o = outs[key]
  line = f"{key} recycles:{o['recycles']} tol:{o['tol']:.2f} pLDDT:{o['pLDDT']:.2f}"
  if 'pTMscore' in o:
    line += f" pTMscore:{o['pTMscore']:.2f}"
  print(line)
  if show_images:
    fig = cf.plot_protein(o['unrelaxed_protein'], Ls=Ls_plot, dpi=100)
    plt.show()
  tmp_pdb_path = os.path.join(I["output_dir"],f'unranked_{key}_unrelaxed.pdb')
  pdb_lines = protein.to_pdb(o['unrelaxed_protein'])
  with open(tmp_pdb_path, 'w') as f: f.write(pdb_lines)

def prep_feats(I, use_turbo=True, clean=True):

  # delete old files
  if clean:
    for f in os.listdir(I["output_dir"]):
      if "rank_" in f: os.remove(os.path.join(I["output_dir"], f))

  #############################
  # homooligomerize
  #############################
  lengths = [len(seq) for seq in I["seqs"]]
  msas_mod, deletion_matrices_mod = cf.homooligomerize_heterooligomer(I["msas"], I["deletion_matrices"],
                                                                      lengths, I["homooligomers"])
  #############################
  # define input features
  #############################
  num_res = len(I["full_sequence"])
  feature_dict = {}
  feature_dict.update(pipeline.make_sequence_features(I["full_sequence"], 'test', num_res))
  feature_dict.update(pipeline.make_msa_features(msas_mod, deletion_matrices=deletion_matrices_mod))
  if not use_turbo:
    feature_dict.update(_placeholder_template_feats(0, num_res))

  ################################
  # set chainbreaks
  ################################
  Ls = []
  for seq,h in zip(I["ori_sequence"].split(":"), I["homooligomers"]):
    Ls += [len(s) for s in seq.split("/")] * h
  Ls_plot = []
  for seq,h in zip(I["seqs"], I["homooligomers"]):
    Ls_plot += [len(seq)] * h

  feature_dict['residue_index'] = cf.chain_break(feature_dict['residue_index'], Ls)
  return feature_dict, Ls_plot